from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import secrets
import ssl
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import FormData
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from open_carrier_portal.admin_store import ADMIN_SESSION_IDLE_SECONDS, AdminStore
from open_carrier_portal.company_access import approve_company_policy
from open_carrier_portal.company_policies import POLICY_LEASE_SECONDS
from open_carrier_portal.config import Settings, is_private_network_host, validate_connector_url
from open_carrier_portal.diagnostics import (
    configure_logging,
    failure_reason,
    install_request_logging,
    log_context,
    log_event,
    log_failure,
    request_reference,
)
from open_carrier_portal.errors import ConnectorError, SourceDatabaseError
from open_carrier_portal.models import ConnectorIdentity
from open_carrier_portal.owner_access import approve_owner_source
from open_carrier_portal.portal_agent import (
    DEFAULT_CONNECTION_FILE,
    PortalConnection,
    pair_portal,
    send_heartbeat,
    serve_live_request,
    validate_portal_url,
)
from open_carrier_portal.repository import Repository, SourceDatabaseProfile
from open_carrier_portal.security import ALLOWED_SCOPES, generate_client_secret, hash_client_secret
from open_carrier_portal.source_contract import (
    BASIC_FIELDS,
    CORE_RESOURCES,
    MAPPING_GROUPS,
    MONITORING_FIELDS,
    OCP_TABLES,
    SOURCE_RESOURCES,
)
from open_carrier_portal.source_credentials import save_password
from open_carrier_portal.source_database import (
    ResourceMapping,
    SourceMapping,
    configured_profile_database,
    configured_source_database,
)

SCOPES = ALLOWED_SCOPES
SECTIONS = (
    "setup",
    "identity",
    "portal",
    "sources",
    "mapping",
    "partners",
    "remote",
    "diagnostics",
    "account",
)
SECTION_LABELS = {
    "portal": "Carrier Portal",
    "sources": "Connections",
    "remote": "Partner connections",
}


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def field(name: str, label: str, value: str = "", *, kind: str = "text") -> str:
    return (
        f'<label>{escape(label)}<input name="{escape(name)}" type="{kind}" '
        f'value="{escape(value)}" maxlength="2000"></label>'
    )


def table(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "<p>No records.</p>"
    keys = list(rows[0])
    return (
        "<div class=scroll><table><thead><tr>"
        + "".join(f"<th>{escape(key)}</th>" for key in keys)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{escape(row.get(key, ''))}</td>" for key in keys) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def configured_settings(settings: Settings, store: AdminStore) -> Settings:
    source = store.get("source")
    monitoring = store.get("observium")
    if not settings.source_database_profile and source.get("name"):
        settings = replace(settings, source_database_profile=source["name"])
    if monitoring and not settings.observium_base_url:
        token = os.getenv(monitoring["token_env"])
        if not token:
            log_event(
                "observium",
                "configuration.token_missing",
                "The selected token variable is absent from the connector service environment. "
                "Set it for the service and restart; do not enter a token on Connections.",
                level=logging.ERROR,
                store=store,
            )
            raise ValueError(
                "The named Observium token environment variable is missing in the connector "
                "service. Set the actual API token in that service's environment, then restart "
                "the connector. Do not enter the token on Connections."
            )
        settings = replace(
            settings,
            observium_base_url=monitoring["url"],
            observium_api_token=token,
            observium_ca_bundle=monitoring.get("ca") or None,
        )
    return settings


def source_for(
    repository: Repository,
    name: str,
    metadata_path: Path,
    mapping: SourceMapping | None = None,
):
    profile = repository.get_source_database_profile(name)
    if profile is None or not profile.enabled:
        raise ValueError("enabled source profile not found")
    return configured_profile_database(profile, metadata_path, mapping)


def http_client(ca: str = "") -> httpx.Client:
    return httpx.Client(
        timeout=15,
        follow_redirects=False,
        trust_env=False,
        verify=ssl.create_default_context(cafile=ca or None),
    )


def create_admin_app(
    settings: Settings,
    *,
    base_url: str = "http://127.0.0.1:8000",
    connection_file: Path = DEFAULT_CONNECTION_FILE,
    configuration_error: str = "",
) -> FastAPI:
    configure_logging()
    base_url = validate_connector_url(base_url, allow_insecure_http=settings.allow_insecure_http)
    parsed = urlsplit(base_url)
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()
    store = AdminStore(settings.database_path)
    store.initialize()
    log_event(
        "app",
        "admin.initialized",
        "Administration initialized.",
        store=store,
    )
    if parsed.scheme == "http" and is_private_network_host(parsed.hostname or ""):
        log_event(
            "app",
            "testing.private_http_enabled",
            "Insecure private-network HTTP testing enabled: admin passwords, sessions and "
            "partner credentials are not encrypted. Do not expose this listener publicly.",
            level=logging.WARNING,
            store=store,
        )
    if settings.allow_insecure_http:
        log_event(
            "portal",
            "testing.private_http_enabled",
            "Private-IP HTTP portal testing enabled: pairing codes and portal credentials "
            "are not encrypted over HTTP. Use only on a trusted network.",
            level=logging.WARNING,
            store=store,
        )

    def monitoring_enabled() -> bool:
        return bool(settings.observium_base_url or store.get("observium").get("url"))

    def portal_ca() -> str:
        return settings.portal_ca_bundle or store.get("portal").get("ca", "")

    def heartbeat() -> None:
        if not connection_file.exists():
            return
        connection = PortalConnection.load(
            connection_file, allow_insecure_http=settings.allow_insecure_http
        )
        identity = repository.get_connector_identity()
        if (
            identity is None
            or not identity.enabled
            or identity.connector_id != connection.connector_id
        ):
            raise ValueError("portal connection does not match enabled connector identity")
        with http_client(portal_ca()) as client:
            send_heartbeat(
                client,
                connection,
                allow_insecure_http=settings.allow_insecure_http,
                repository=repository,
            )
        store.save("heartbeat", {"seen": str(time.time())})

    async def agent_loop() -> None:
        while True:
            with log_context(store):
                try:
                    await run_in_threadpool(heartbeat)
                except (ValueError, OSError, httpx.HTTPError) as exc:
                    log_failure("portal", "heartbeat.failed", exc)
                    store.save(
                        "heartbeat",
                        {"error": "Heartbeat failed; test or reconnect on the Portal page."},
                    )
            await asyncio.sleep(30)

    def serve_live() -> None:
        if not connection_file.exists() or not store.get("heartbeat").get("seen"):
            return
        connection = PortalConnection.load(
            connection_file, allow_insecure_http=settings.allow_insecure_http
        )
        identity = repository.get_connector_identity()
        if (
            identity is None
            or not identity.enabled
            or identity.connector_id != connection.connector_id
        ):
            raise ValueError("live requests require an enabled paired connector identity")
        with http_client(portal_ca()) as client:
            serve_live_request(
                client,
                connection,
                repository=repository,
                settings=settings,
                settings_loader=lambda: configured_settings(settings, store),
            )

    async def live_loop() -> None:
        while True:
            with log_context(store):
                try:
                    await run_in_threadpool(serve_live)
                except (ValueError, OSError, httpx.HTTPError) as exc:
                    log_failure("portal", "live.transport_failed", exc)
                    await asyncio.sleep(5)
            await asyncio.sleep(0.25)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(agent_loop())
        live_task = asyncio.create_task(live_loop())
        log_event(
            "app", "service.started", "Connector administration service started.", store=store
        )
        try:
            yield
        finally:
            log_event("app", "service.stopping", "Connector service is stopping.", store=store)
            task.cancel()
            live_task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(asyncio.CancelledError):
                await live_task

    app = FastAPI(title="Carrier Connector Administration", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed.hostname])
    app.state.repository = repository
    app.state.admin_store = store

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            "ocp_admin",
            token,
            httponly=True,
            samesite="strict",
            secure=parsed.scheme == "https",
            max_age=ADMIN_SESSION_IDLE_SECONDS,
        )

    @app.middleware("http")
    async def renew_active_session(request: Request, call_next: RequestResponseEndpoint):
        response = await call_next(request)
        token = getattr(request.state, "admin_activity", "")
        if token and await run_in_threadpool(store.renew_session, token):
            set_session_cookie(response, token)
        return response

    def page(
        title: str,
        content: str,
        csrf: str,
        *,
        status: int = 200,
        signed_in: bool = True,
        username: str = "",
    ):
        if status >= 400:
            reference = request_reference()
            if reference:
                content += f"<p>Diagnostic reference: <code>{escape(reference)}</code>.</p>"
            if signed_in:
                content += '<p><a href="/admin/diagnostics">View Diagnostics logs</a></p>'
        if signed_in and configuration_error:
            content = f'<p class="notice">{escape(configuration_error)}</p>' + content
        nav = (
            "<nav>"
            + "".join(
                f'<a href="/admin/{s}">{escape(SECTION_LABELS.get(s, s.title()))}</a>'
                for s in SECTIONS
            )
            + form("logout", csrf, "", "Sign out")
            + "</nav>"
            if signed_in
            else ""
        )
        session_ui = (
            f'<meta name="ocp-admin-csrf" content="{escape(csrf)}">'
            f'<meta name="ocp-admin-username" content="{escape(username)}">'
            f'<meta name="ocp-admin-idle-timeout" content="{ADMIN_SESSION_IDLE_SECONDS}">'
            '<div id="admin-session-notice" class="notice" role="status" hidden></div>'
            '<script src="/admin/assets/session.js" defer></script>'
            if signed_in
            else ""
        )
        return HTMLResponse(
            f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} | Connector administration</title><style>
body{{margin:0;background:#f1f5f9;color:#172b3a;font:16px system-ui,sans-serif}}
main{{max-width:1100px;margin:2rem auto;padding:0 1rem}}
nav,section{{background:white;border:1px solid #ccd8e3;border-radius:10px;
padding:1.3rem;margin:1rem 0}}
nav a{{display:inline-block;margin:.4rem .9rem .4rem 0;color:#075985}}
label{{display:block;margin:.8rem 0}}input,select,textarea,button{{font:inherit;padding:.55rem}}
input,textarea{{display:block;width:95%;max-width:44rem;border:1px solid #94a3b8;border-radius:5px}}
textarea{{height:20rem}}button{{background:#075985;color:white;border:0;border-radius:5px;
margin:.8rem 0;cursor:pointer}}
table{{border-collapse:collapse;width:100%}}
td,th{{text-align:left;padding:.6rem;border-bottom:1px solid #ddd}}
.scroll{{overflow:auto}}.notice{{padding:1rem;background:#fff7df;border-left:4px solid #c88c00}}
#admin-session-notice{{position:sticky;top:.5rem;z-index:10}}
#admin-session-notice a,#admin-session-notice button{{margin-left:.5rem}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}small{{color:#475569}}
.portal-intro{{margin-bottom:1.8rem;max-width:44rem;color:#475569;line-height:1.65}}
.portal-grid{{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(240px,1fr);gap:2rem}}
.portal-steps{{list-style:none;padding:0;margin:0;counter-reset:portal-step}}
.portal-step{{display:flex;gap:1rem;padding:0 0 1.8rem;counter-increment:portal-step}}
.portal-step::before{{content:counter(portal-step);flex:0 0 2.3rem;height:2.3rem;
border-radius:50%;background:#e0f2fe;color:#075985;display:grid;place-items:center;font-weight:700}}
.portal-step h3{{margin:.2rem 0 .65rem;font-size:1.1rem}}
.portal-step p{{line-height:1.65;margin:.6rem 0;color:#475569}}
.portal-step-content{{min-width:0;flex:1}}
.portal-button{{display:inline-block;background:#075985;color:white;text-decoration:none;
padding:.7rem 1rem;border-radius:6px;font-weight:600}}
.portal-button:hover,button:hover{{background:#0c4a6e}}
.portal-summary{{align-self:start;padding:1.4rem;border:1px solid #dbe5ee;
border-radius:10px;background:#f8fafc}}
.portal-summary h3{{margin-top:0}}.portal-summary p{{line-height:1.65;color:#475569}}
.portal-badge{{display:inline-block;padding:.35rem .7rem;border-radius:20px;
background:#e2e8f0;color:#334155;font-size:.85rem;font-weight:600}}
.portal-badge.connected{{background:#dcfce7;color:#166534}}
.portal-note{{font-size:.9rem}}.portal-note strong{{color:#334155}}
.portal-code{{box-sizing:border-box;width:100%;font-family:ui-monospace,monospace;
padding:.8rem;letter-spacing:.04em}}
.portal-step label{{font-weight:600}}.portal-step button{{padding:.75rem 1rem;font-weight:600}}
.portal-warning{{padding:.8rem 1rem;background:#fff7df;border-radius:6px;line-height:1.6}}
.portal-link{{color:#075985}}.portal-summary button{{font-size:.9rem}}
.setup-intro{{color:#475569;line-height:1.65;margin:0 0 1.5rem}}
.setup-badge{{display:inline-block;border-radius:20px;padding:.3rem .65rem;
font-size:.78rem;font-weight:600;background:#f1f5f9;color:#475569;max-width:100%;
box-sizing:border-box;overflow-wrap:anywhere}}
.setup-badge.good{{background:#dcfce7;color:#166534}}
.setup-badge.attention{{background:#fef3c7;color:#854d0e}}
.setup-badge.selected{{background:#e0f2fe;color:#075985}}
.setup-steps{{padding:0;margin:0;list-style:none;counter-reset:setup-step}}
.setup-card{{position:relative;counter-increment:setup-step;border:1px solid #dbe5ee;
border-radius:10px;padding:1.2rem 1.2rem 1.2rem 4.2rem;margin-bottom:1rem}}
.setup-card::before{{content:counter(setup-step);position:absolute;left:1rem;top:1.2rem;
width:2.2rem;height:2.2rem;border-radius:50%;display:grid;place-items:center;
background:#f1f5f9;color:#475569;font-weight:700}}
.setup-card-head{{display:flex;align-items:center;justify-content:space-between;
gap:.7rem;flex-wrap:wrap}}
.setup-card h3{{margin:0;font-size:1.05rem}}.setup-card p{{font-size:.93rem;
line-height:1.65;color:#475569;margin:.65rem 0;overflow-wrap:anywhere}}
.setup-card a{{display:inline-block;margin-top:.2rem;color:#075985;font-weight:600;
font-size:.9rem;text-decoration:none}}
.setup-card a:hover{{text-decoration:underline}}
.setup-card .setup-secondary{{margin-left:1.2rem;font-weight:400}}
.setup-verify{{background:#f8fafc}}
.setup-verify form{{margin:0}}.setup-verify button{{font-size:.9rem;font-weight:600;
padding:.7rem 1rem;margin:.4rem 0}}
.setup-footnote{{font-size:.85rem;line-height:1.65;color:#64748b;margin-top:1.3rem}}
.connection-card{{max-width:48rem;border:1px solid #dbe5ee;border-radius:10px;
padding:1.5rem;margin:1rem 0 1.5rem}}
.connection-card h3{{margin-top:0}}.connection-card p{{line-height:1.65;color:#475569}}
.connection-fields{{display:grid;grid-template-columns:1fr 1fr;gap:0 1.2rem}}
.connection-fields label{{min-width:0}}.connection-fields input{{box-sizing:border-box;width:100%}}
.connection-card details{{border-top:1px solid #e2e8f0;padding:1rem 0;margin-top:1rem}}
.connection-card summary{{cursor:pointer;font-weight:600;color:#075985}}
.connection-card pre{{font-size:.85rem;padding:1rem;background:#f1f5f9}}
.connection-actions{{display:flex;gap:.7rem;flex-wrap:wrap}}
.connection-actions .secondary{{background:#e0f2fe;color:#075985}}
.connection-list{{display:flex;gap:.8rem;flex-wrap:wrap;margin:.8rem 0}}
.connection-list a{{color:#075985}}.connection-managed{{padding:.7rem;background:#f0f9ff;
border-radius:6px;font-size:.9rem}}
.mapper-toolbar{{display:flex;align-items:end;gap:1rem;flex-wrap:wrap}}
.mapper-toolbar label{{flex:1}}.mapper-toolbar select{{width:100%;box-sizing:border-box}}
.mapper-grid{{display:grid;grid-template-columns:minmax(210px,1fr) minmax(340px,1.8fr);
gap:1.2rem;margin:1rem 0}}.mapper-panel{{border:1px solid #dbe5ee;border-radius:10px;
padding:1.2rem;min-width:0}}.mapper-panel h3{{margin-top:0}}
.mapper-tables,.mapper-targets{{height: min(65vh,46rem);overflow:auto;padding-right:.4rem}}
.mapper-tables details{{margin:.6rem 0;border:1px solid #dbe5ee;border-radius:8px;padding:.7rem}}
.mapper-tables summary{{cursor:pointer;font-weight:600;overflow-wrap:anywhere}}
.mapper-linked{{font-size:.75rem;color:#0369a1;margin-left:.5rem}}
.mapper-ocp-table{{border:1px solid #cbd5e1;border-radius:9px;margin:0 0 1rem;background:#fff}}
.mapper-ocp-table>summary{{cursor:pointer;padding:1rem;background:#f0f9ff;
font-weight:700;color:#0c4a6e;border-radius:9px}}
.mapper-help-button{{margin:0 0 0 .7rem;padding:.1rem .5rem;min-width:1.8rem;
border:1px solid #7dd3fc;border-radius:50%;background:#fff;color:#075985;font-weight:700}}
.mapper-help-button:focus-visible{{outline:2px solid #0369a1;outline-offset:3px}}
.mapper-hint{{width:min(38rem,calc(100vw - 2rem));max-height:75vh;overflow:auto;
box-sizing:border-box;padding:1.3rem;border:1px solid #94a3b8;border-radius:10px;
background:#fff;color:#334155;box-shadow:0 10px 40px #0f172a33}}
.mapper-hint h3{{margin-top:0}}.mapper-hint p{{line-height:1.6}}
.mapper-hint table{{width:100%;table-layout:fixed}}.mapper-hint code{{overflow-wrap:anywhere}}
.mapper-section{{padding:.6rem .8rem}}
.mapper-api-fields{{padding:.6rem .8rem;border-top:1px solid #e2e8f0}}
.mapper-api-fields h4{{margin:.5rem 0}}.mapper-api-field{{display:flex;align-items:center;
justify-content:space-between;gap:.7rem;padding:.7rem 0;border-bottom:1px solid #e2e8f0;
overflow-wrap:anywhere}}.mapper-api-field code{{min-width:0}}
.mapper-api-badge{{color:#475569;background:#f1f5f9;padding:.2rem .4rem;border-radius:4px;
font-size:.75rem;white-space:nowrap}}
.mapper-section>summary{{cursor:pointer;font-weight:600;font-size:.9rem;padding:.4rem 0}}
.mapper-section.active>summary{{color:#0369a1}}.mapper-extra>summary{{cursor:pointer;
font-size:.8rem;padding:.6rem 0;color:#475569}}
.mapper-field{{display:block;width:100%;text-align:left;box-sizing:border-box;background:#f1f5f9;
color:#334155;margin:.4rem 0;padding:.5rem .7rem;border:1px solid #dbe5ee;overflow-wrap:anywhere}}
.mapper-field:hover,.mapper-field[aria-pressed="true"]{{background:#e0f2fe;color:#075985}}
.mapper-slot{{display:grid;grid-template-columns:minmax(0,1fr) 1.5rem minmax(0,1fr);
align-items:center;gap:.4rem;margin:.6rem 0}}
.mapper-drop{{padding:.7rem;border:1px dashed #94a3b8;border-radius:7px;
min-width:0;background:#f8fafc}}.mapper-drop.linked{{border-style:solid;border-color:#7dd3fc;
background:#f0f9ff}}.mapper-drop.over{{background:#e0f2fe;border-color:#0284c7}}
.mapper-drop code{{display:block;font-size:.8rem}}.mapper-arrow{{text-align:center;
color:#0284c7;font-size:1.3rem}}.mapper-target{{min-width:0;overflow-wrap:anywhere}}
.mapper-target{{padding:.7rem;border:1px solid #dbe5ee;border-radius:7px;background:#fff}}
.mapper-target code{{display:block;font-size:.85rem;font-weight:700;overflow-wrap:anywhere}}
.mapper-target small{{display:block;color:#64748b;font-size:.75rem}}
.mapper-row-headings{{display:grid;grid-template-columns:minmax(0,1fr) 1.5rem minmax(0,1fr);
gap:.4rem;padding:.7rem .8rem 0;font-size:.8rem;color:#475569;font-weight:600}}
.mapper-slot-actions{{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.5rem}}
.mapper-slot button{{font-size:.8rem;margin:0;padding:.35rem .6rem}}
.mapper-slot code{{overflow-wrap:anywhere}}.mapper-slot strong{{font-size:.9rem}}
.mapper-help{{font-size:.9rem;color:#475569;line-height:1.6}}
.mapper-join{{padding:1rem;background:#f8fafc;border:1px solid #dbe5ee;
border-radius:8px;margin:.8rem 0}}.mapper-join label{{font-size:.9rem}}
.mapper-join select{{max-width:100%;box-sizing:border-box}}.mapper-message{{padding:.8rem;
border-radius:6px;background:#f0f9ff;margin:1rem 0;overflow-wrap:anywhere}}
.mapper-message.error{{background:#fff1f2;color:#9f1239}}
.mapper-actions{{display:flex;gap:.7rem;flex-wrap:wrap}}.mapper-sql{{background:#f1f5f9;
padding:1rem;font-size:.85rem}}.mapper-preview{{overflow:auto;max-height:24rem}}
.setup-card a:focus-visible,
.portal-button:focus-visible{{outline:3px solid #38bdf8;outline-offset:4px}}
@media(max-width:720px){{.portal-grid{{grid-template-columns:1fr}}
.connection-fields{{grid-template-columns:1fr}}.connection-card{{padding:1rem}}
.mapper-grid{{grid-template-columns:1fr}}.mapper-tables{{height:18rem}}
.mapper-targets{{height:36rem}}.mapper-panel{{padding:.8rem}}
.setup-card{{padding-left:3.6rem}}.setup-card::before{{left:.7rem}}
.setup-card .setup-secondary{{display:block;margin:.6rem 0 0}}
.portal-step{{gap:.7rem}}main{{margin:1rem auto}}}}
</style><main><h1>Carrier connector</h1><p>Local administration. Hosted portal is optional.</p>
{session_ui}{nav}<section><h2>{escape(title)}</h2>{content}</section></main></html>""",
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                    "script-src 'self'; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'"
                ),
                "Referrer-Policy": "same-origin",
            },
        )

    def form(action: str, csrf: str, content: str, button: str = "Save") -> str:
        return (
            f'<form method="post" action="/admin/{action}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">{content}'
            f"<button>{escape(button)}</button></form>"
        )

    def company_directory(selected_profile: str, profiles: list[dict[str, str]], csrf: str) -> str:
        name = (
            selected_profile
            or settings.source_database_profile
            or store.get("source").get("name", "")
        )
        content = (
            "<h3>Companies from your database</h3>"
            "<p>Read directly through the saved Companies mapping. The company ID is the "
            "source ID in your database, not a partner ID from another connector. "
            "Listing companies does not create partners or grant access.</p>"
            '<form method="get" action="/admin/partners"><label>Database connection'
            '<select name="profile"><option value="">Choose a database connection</option>'
            + "".join(
                f'<option value="{escape(profile["name"])}"'
                f"{' selected' if profile['name'] == name else ''}>"
                f"{escape(profile['name'])}"
                f"{' (disabled)' if profile['enabled'] != '1' else ''}</option>"
                for profile in profiles
            )
            + '</select></label><button type="submit">Load companies</button></form>'
        )
        if not name:
            return content + "<p>Select a database connection to load its mapped companies.</p>"
        mapping_url = "/admin/mapping?profile=" + quote(name, safe="")
        try:
            source = source_for(repository, name, settings.database_path)
            if source.missing_fields_for(requirements={"companies": ("source_id", "name")}):
                return (
                    content + "<p>Save a Companies mapping with both Company ID (source_id) and "
                    f'Company name (name) first. <a href="{mapping_url}">Open mapping</a>.</p>'
                )
            rows = source.fetch("companies", limit=101)
            if any(
                row[field] is None or not str(row[field]).strip()
                for row in rows
                for field in ("source_id", "name")
            ):
                raise SourceDatabaseError("Mapped companies require non-empty IDs and names")
        except (ValueError, SourceDatabaseError, OSError) as exc:
            log_failure("database", "companies.load_failed", exc)
            return (
                content + '<p class="notice" role="alert">Could not load mapped companies. '
                "Check the database connection and saved Companies mapping. "
                f"Diagnostic reference: <code>{escape(request_reference())}</code>. "
                '<a href="/admin/diagnostics">View Diagnostics logs</a>.</p>'
            )
        log_event(
            "database",
            "companies.loaded",
            "Loaded mapped companies for the partner administration page.",
            rows_shown=min(len(rows), 100),
            truncated=int(len(rows) > 100),
        )
        if not rows:
            return content + "<p>No companies were returned by the saved Companies mapping.</p>"
        approval = ""
        if connection_file.exists():
            try:
                portal = PortalConnection.load(
                    connection_file, allow_insecure_http=settings.allow_insecure_http
                )
                proposals = [
                    local
                    for local in repository.list_portal_policies(portal)
                    if local.policy.state in {"pending", "needs_approval"}
                    and not local.was_approved
                    and time.time() - local.synced_at <= POLICY_LEASE_SECONDS
                ]
            except (ValueError, OSError) as exc:
                log_failure("portal", "policies.load_failed", exc)
                return content + (
                    '<p role="alert">Cannot load portal approvals. Check Carrier Portal '
                    'pairing and <a href="/admin/diagnostics">Diagnostics</a>.</p>'
                )
            if proposals:
                approval = form(
                    "approve-company-policy",
                    csrf,
                    f'<input type="hidden" name="profile" value="{escape(name)}">'
                    '<label>Portal sharing proposal<select name="policy">'
                    + "".join(
                        f'<option value="{local.policy.policy_id}@{local.policy.revision}">'
                        f"{escape(local.policy.recipient_name)} - revision "
                        f"{local.policy.revision} - "
                        + (
                            "FULL LAYOUT"
                            if local.policy.visibility == "full_shared_path" else "endpoints"
                        )
                        + "</option>"
                        for local in proposals
                    )
                    + "</select></label>"
                    + field("company", "Company ID from this database")
                    + '<label><input type="checkbox" name="consent" value="yes" required>'
                    "Approve this company's current and future billed circuits, endpoints "
                    "and linked monitoring status for the selected portal recipient.</label>"
                    '<label><input type="checkbox" name="layout_consent" value="yes">'
                    "For a FULL LAYOUT proposal, also approve all intermediate ports, patches, "
                    "pseudowires, dark-fiber descriptions and their device/rack/site context "
                    "and Observium status. "
                    "This does not grant independent inventory browsing.</label>"
                    "<p>The ID is validated against this database; a company name is never "
                    "used as identity. Approval grants no independent device/port access.</p>",
                    "Approve company link and circuit policy",
                )
        return (
            content
            + (
                "<p>Showing the first 100 companies; more are available in your database. "
                "This directory preview is limited to 100 rows.</p>"
                if len(rows) > 100
                else f"<p>{len(rows)} mapped companies.</p>"
            )
            + table(
                [
                    {
                        "Company ID in your database": str(row["source_id"]),
                        "Company": str(row["name"]),
                    }
                    for row in rows[:100]
                ]
            )
            + approval
        )

    def portal_policy_content(csrf: str) -> str:
        content = (
            "<h3>Portal company-sharing approvals</h3>"
            "<p>Manage proposals and revocation in Carrier Portal &rarr; Permissions. "
            "Here the data owner selects and approves the database company, or stops sharing. "
            "Source records stay in your database. Approved partner circuit lists and "
            "endpoint/status reads are available through the portal's live JSON APIs.</p>"
        )
        if not connection_file.exists():
            return content + '<p><a href="/admin/portal">Pair Carrier Portal</a> first.</p>'
        try:
            portal = PortalConnection.load(
                connection_file, allow_insecure_http=settings.allow_insecure_http
            )
            policies = repository.list_portal_policies(portal)
        except (ValueError, OSError) as exc:
            log_failure("portal", "policies.load_failed", exc)
            return content + (
                '<p role="alert">Cannot load sharing approvals. Check Carrier Portal '
                'pairing and <a href="/admin/diagnostics">Diagnostics</a>.</p>'
            )
        if not policies:
            return content + (
                "<p>No proposals received. Propose sharing on the portal, then "
                '<a href="/admin/portal">Check connection</a> or wait for the next heartbeat.</p>'
            )
        for local in policies:
            policy = local.policy
            state = (
                "Offline - approval cannot authorize requests"
                if time.time() - local.synced_at > POLICY_LEASE_SECONDS
                else (
                    "Approved locally; waiting for portal acknowledgement"
                    if local.receipt and policy.state != "active"
                    else policy.state
                )
            )
            if local.was_approved and local.receipt is None and policy.state != "revoked":
                state += " - stopped or mapping changed; new approval required"
            elif policy.state == "active" and local.receipt is None:
                state = "No local approval - request a new revision in the portal"
            view_label = (
                "Full circuit layout"
                if policy.visibility == "full_shared_path" else "Endpoints only"
            )
            content += (
                f"<h4>{escape(policy.recipient_name)} - revision {policy.revision}</h4>"
                f"<p>{escape(state)} &mdash; "
                f"{view_label}</p>"
            )
            if local.receipt:
                content += (
                    f"<p>Database profile ID: {escape(local.receipt.source_profile_id)}; "
                    f"company ID: {escape(local.receipt.source_company_id)}</p>"
                )
                content += form(
                    "stop-company-policy",
                    csrf,
                    f'<input type="hidden" name="policy" value="{policy.policy_id}">',
                    "Stop sharing immediately",
                )
        return content

    def render(section: str, csrf: str, selected_profile: str = "") -> str:
        identity = repository.get_connector_identity()
        snapshot = repository.administration_snapshot()
        if section == "setup":
            source = settings.source_database_profile or store.get("source").get("name", "")
            profile = repository.get_source_database_profile(source) if source else None
            identity_ready = bool(identity and identity.enabled)
            source_selected = bool(profile and profile.enabled)
            portal = store.get("heartbeat")
            paired = connection_file.exists()
            connected = bool(
                paired
                and not portal.get("error")
                and portal.get("seen")
                and time.time() - float(portal["seen"]) < 90
            )
            portal_status = (
                "Connected"
                if connected
                else "Needs attention"
                if paired and portal.get("error")
                else "Paired"
                if paired
                else "Not connected"
            )
            steps = (
                (
                    "identity",
                    "Company identity",
                    "Configured" if identity_ready else "Disabled" if identity else "Get started",
                    "good" if identity_ready else "attention" if identity else "",
                    f"This connector belongs to {identity.organization_name}. "
                    "Keep your company details up to date."
                    if identity_ready
                    else "Set your company name so partners can identify this connector. "
                    "An enabled company identity is also needed for portal pairing.",
                    "Review company" if identity else "Set up company",
                ),
                (
                    "sources",
                    "Set up your connections",
                    "Selected"
                    if source_selected
                    else "Profile disabled"
                    if profile
                    else "Profile missing"
                    if source
                    else "Not selected",
                    "selected" if source_selected else "attention" if source else "",
                    "A database profile is selected. Test its connection before sharing data."
                    if source_selected
                    else f"The selected profile ({source}) is missing or disabled. "
                    "Review your source settings before continuing."
                    if source
                    else "Add your database and monitoring connections. You can manage "
                    "existing connections or add more as your setup grows.",
                    "Manage connections",
                ),
                (
                    "mapping",
                    "Check your database mapping",
                    "Review mapping" if source_selected else "After source setup",
                    "",
                    "Match your database tables and columns to the connector's fields. "
                    "Validate the mapping and preview a few records before sharing.",
                    "Review field mapping",
                ),
                (
                    "portal",
                    "Carrier Portal (optional)",
                    portal_status,
                    "good" if connected else "attention" if paired and portal.get("error") else "",
                    "Link this connector to your company account with a one-time pairing code. "
                    "Not required for direct partner sharing. Currently provides registration "
                    "and online status, plus owner-approved company policy synchronization.",
                    "View portal connection" if paired else "Connect to Carrier Portal",
                ),
            )
            cards = "".join(
                '<li class="setup-card"><div class="setup-card-head">'
                f"<h3>{escape(title)}</h3>"
                f'<span class="setup-badge {tone}">{escape(status)}</span></div>'
                f"<p>{escape(description)}</p>"
                f'<a href="/admin/{route}">{escape(action)} &rarr;</a>'
                + (
                    '<a class="setup-secondary" href="/admin/remote">'
                    "Manage partner connections &rarr;</a>"
                    if route == "sources"
                    else ""
                )
                + "</li>"
                for route, title, status, tone, description, action in steps
            )
            return (
                '<p class="setup-intro">Complete these steps to set up your connector. '
                "You can return to any step later to update your settings.</p>"
                f'<ol class="setup-steps">{cards}'
                '<li class="setup-card setup-verify"><div class="setup-card-head">'
                '<h3>Verify your setup</h3><span class="setup-badge">Run checks</span></div>'
                "<p>A saved source profile is not a health check. Run validation to check "
                "the selected source connection and mapped columns.</p>"
                + form("readiness", csrf, "", "Run readiness checks")
                + '<p><a class="portal-link" href="/admin/diagnostics">View diagnostics &rarr;</a>'
                '</p></li><li class="setup-card"><div class="setup-card-head">'
                '<h3>Choose what to share</h3><span class="setup-badge">Your control</span></div>'
                "<p>Add trusted partners and grant only the resources they need. "
                "Circuit access also requires an explicit circuit share. "
                "No partner access is granted just by completing setup.</p>"
                '<a href="/admin/partners">Manage partner access &rarr;</a></li></ol>'
                '<p class="setup-footnote">After selecting a database or changing monitoring '
                "settings, restart the connector service to apply them. "
                "A portal heartbeat does not confirm source-database health.</p>"
            )
        if section == "identity":
            return (
                "<p>Existing identity is preserved; editing the name does not rotate IDs.</p>"
                + table(
                    [
                        {
                            "organization_id": identity.organization_id,
                            "connector_id": identity.connector_id,
                        }
                    ]
                )
                if identity
                else "<p>Initialize your company. No partners or grants are created.</p>"
            ) + form(
                "identity",
                csrf,
                field("name", "Company name", identity.organization_name if identity else ""),
            )
        if section == "portal":
            paired = connection_file.exists()
            portal = None
            portal_url = settings.portal_url
            connection_error = ""
            if paired:
                try:
                    portal = PortalConnection.load(
                        connection_file, allow_insecure_http=settings.allow_insecure_http
                    )
                    portal_url = portal.portal_url
                except (ValueError, OSError) as exc:
                    log_failure("portal", "saved_connection.failed", exc)
                    connection_error = (
                        "The saved connection could not be read. Disconnect it locally, "
                        "then obtain a new pairing code."
                    )
            state = store.get("heartbeat")
            recent = bool(paired and state.get("seen") and time.time() - float(state["seen"]) < 90)
            error = connection_error or state.get("error", "")
            status = (
                "Connection needs attention"
                if paired and error
                else "Connected"
                if recent
                else "Paired &mdash; waiting for a heartbeat"
                if paired
                else "Not connected"
            )
            open_portal = (
                f'<a class="portal-button" href="{escape(portal_url)}/portal/connector" '
                'target="_blank" rel="noopener noreferrer">Open Carrier Portal &nearr;</a>'
                if portal_url
                else '<p class="portal-warning"><strong>Portal connection is not configured yet.'
                "</strong><br>Your connector administrator needs to configure the "
                "Carrier Portal address before you can connect.</p>"
            )
            if paired:
                code_form = (
                    "<p>This connector is already paired. Use Check connection to verify "
                    "it, or disconnect before entering a new code.</p>"
                )
            elif not settings.portal_url:
                code_form = (
                    "<p>Pairing will be available once the portal address is configured.</p>"
                )
            elif identity is None or not identity.enabled:
                code_form = (
                    '<p class="portal-warning"><a class="portal-link" href="/admin/identity">'
                    "Set up your company identity</a> before connecting to Carrier Portal.</p>"
                )
            else:
                code_form = form(
                    "pair",
                    csrf,
                    '<label for="pairing-code">Pairing code</label>'
                    '<input class="portal-code" id="pairing-code" name="code" type="password" '
                    'autocomplete="one-time-code" spellcheck="false" required '
                    'minlength="40" maxlength="128" placeholder="Paste your pairing code here">'
                    '<p class="portal-note">Use the code from Carrier Portal, not your '
                    "account password.</p>",
                    "Connect to Carrier Portal",
                )
            controls = ""
            if paired:
                controls = form("heartbeat", csrf, "", "Check connection") + form(
                    "disconnect",
                    csrf,
                    '<p class="portal-note">Disconnecting stops this connector from checking in. '
                    "Also disconnect it in Carrier Portal to revoke its credential.</p>",
                    "Disconnect locally",
                )
            status_detail = (
                "A heartbeat was received within the last 90 seconds."
                if recent and not error
                else "The connector checks in automatically every 30 seconds."
                if paired
                else "Your connector has not been linked to a Carrier Portal account."
            )
            owner_sources = (
                "<section><h2>My company inventory in Carrier Portal</h2>"
                "<p>Explicitly enable each database your portal company users may browse. "
                "This exposes its full circuit inventory, including circuits billed to partners, "
                "with endpoint details and linked monitoring only. It does not grant partners "
                "any additional access. No raw tables, private management fields or writes.</p>"
            )
            if (
                portal is not None
                and identity is not None
                and identity.enabled
                and identity.connector_id == portal.connector_id
            ):
                enabled = repository.list_portal_owner_sources(portal)
                options = "".join(
                    f'<option value="{escape(p["name"])}">{escape(p["name"])}</option>'
                    for p in snapshot["profiles"]
                    if p["enabled"] == "1"
                )
                owner_sources += form(
                    "enable-owner-source",
                    csrf,
                    '<label>Database<select name="profile" required>'
                    + options
                    + "</select></label>"
                    '<label><input type="checkbox" name="consent" value="yes" required>'
                    "I approve full circuit inventory access for our portal company users."
                    "</label>"
                    '<label><input type="checkbox" name="explorer_consent" value="yes">'
                    "Also enable our company hierarchy explorer: sites, racks, devices, ports, "
                    "company relationships and related circuits (read-only safe fields)."
                    "</label>"
                    '<label><input type="checkbox" name="layout_consent" value="yes">'
                    "Also enable full circuit layouts: intermediate ports, patches, pseudowires, "
                    "dark-fiber descriptions and linked Observium status (read-only safe fields)."
                    "</label>"
                    "<p>Existing circuit-only approvals do not enable these broader views. "
                    "Re-pairing, disabling or changing the database/mapping requires "
                    "fresh approval. Up to 100 databases per pairing.</p>",
                    "Enable own-company browsing",
                )
                for source in enabled:
                    owner_sources += (
                        f"<p><strong>{escape(source.name)}</strong> &mdash; enabled"
                        + (" with full circuit layouts" if source.layout_enabled else "")
                        + (
                            " with hierarchy explorer</p>"
                            if source.explorer_enabled
                            else " (circuits only)</p>"
                        )
                        + form(
                            "stop-owner-source",
                            csrf,
                            '<input type="hidden" name="profile_id" '
                            f'value="{source.source_profile_id}">',
                            "Stop own-company browsing",
                        )
                    )
                if not enabled:
                    owner_sources += "<p>No databases enabled for own-company browsing.</p>"
            else:
                owner_sources += "<p>Pair an enabled connector before approving a database.</p>"
            owner_sources += "</section>"
            return (
                '<p class="portal-intro">Link this connector to your company&rsquo;s '
                "Carrier Portal account in three simple steps. "
                "You can also use direct partner sharing without connecting to the portal.</p>"
                + (
                    '<p class="portal-warning"><strong>Private-network HTTP testing enabled.'
                    "</strong> Pairing codes and portal credentials are not encrypted over HTTP. "
                    "Use only on a trusted network; switch to HTTPS for production.</p>"
                    if settings.allow_insecure_http
                    else ""
                )
                + '<div class="portal-grid"><ol class="portal-steps">'
                '<li class="portal-step"><div class="portal-step-content">'
                "<h3>Open Carrier Portal</h3>"
                "<p>Sign in with your <strong>company administrator account</strong>, "
                "then select <strong>Connector</strong> from the navigation. "
                "The button below opens that page in a new tab.</p>"
                + open_portal
                + '</div></li><li class="portal-step"><div class="portal-step-content">'
                "<h3>Get your pairing code</h3>"
                "<p>Select <strong>Generate pairing code / replace connector</strong> "
                "in Carrier Portal, then copy the code shown.</p>"
                '<p class="portal-note">The code is shown only once, can be used once, '
                "and expires after <strong>15 minutes</strong>.</p>"
                '<p class="portal-warning"><strong>Replacing a connector?</strong> '
                "Generating a new code immediately disconnects the previously paired connector."
                '</p></div></li><li class="portal-step"><div class="portal-step-content">'
                "<h3>Paste your code here</h3>"
                + code_form
                + '</div></li></ol><aside class="portal-summary"><h3>Your connection</h3>'
                + f'<span class="portal-badge{" connected" if recent and not error else ""}" '
                f'role="status">{status}</span><p>{status_detail}</p>'
                + (f'<p class="portal-warning">{escape(error)}</p>' if error else "")
                + controls
                + "<hr><h3>Your credentials stay here</h3>"
                "<p>Your database password and monitoring credentials stay on "
                "this connector. They are not sent to Carrier Portal.</p>"
                '<p class="portal-note"><strong>Available today:</strong> connector '
                "registration, online status and company-policy approval synchronization. "
                'Approve proposals on <a href="/admin/partners">Partners</a>. '
                "Approved partner circuit lists and endpoint/status reads are available live "
                "through the portal's Browse Data interface.</p></aside></div>" + owner_sources
            )
        if section in {"sources", "mapping"}:
            name = (
                selected_profile
                or settings.source_database_profile
                or store.get("source").get("name")
                or (
                    "nettool"
                    if any(p["name"] == "nettool" for p in snapshot["profiles"])
                    else "database"
                )
            )
            profile = repository.get_source_database_profile(name)
            if section == "mapping":
                if profile is None and not selected_profile and snapshot["profiles"]:
                    name = snapshot["profiles"][0]["name"]
                    profile = repository.get_source_database_profile(name)
                mapping = profile.mapping_json if profile else SourceMapping({}).to_json()
                return (
                    '<p class="setup-intro">Your database is on the left. OCP tables are on '
                    "the right. Drag a database field into the empty box beside its OCP field: "
                    "<strong>Database field &rarr; OCP field</strong>.</p>"
                    f'<div id="mapping-workspace" data-profile="{escape(name)}" '
                    f'data-csrf="{escape(csrf)}">'
                    '<div class="mapper-toolbar"><label>Database connection'
                    '<select id="map-profile">'
                    + "".join(
                        f'<option value="{escape(p["name"])}"'
                        f"{' selected' if p['name'] == name else ''}>{escape(p['name'])}</option>"
                        for p in snapshot["profiles"]
                    )
                    + "</select></label>"
                    "</div>"
                    '<p id="map-message" class="mapper-message" '
                    'role="status" aria-live="polite">Loading database schema...</p>'
                    '<div class="mapper-grid"><div class="mapper-panel">'
                    "<h3>Linked database tables</h3><label>Find a table or field"
                    '<input id="map-search" type="search"></label>'
                    '<div id="map-tables" class="mapper-tables"></div></div>'
                    '<div class="mapper-panel"><h3>OCP tables '
                    '<span id="map-target-count"></span></h3>'
                    "<label>Find an OCP table or field"
                    '<input id="map-target-search" type="search"></label>'
                    '<p class="mapper-help">Drop on the left side of a row to link it to the '
                    "OCP field on the right. Or select a database field and choose Assign.</p>"
                    '<div id="map-slots" class="mapper-targets"></div></div></div>'
                    '<p id="map-active-label" class="mapper-help"></p>'
                    '<div class="mapper-actions">'
                    '<button id="map-preview" type="button">Preview 5 mapped rows</button>'
                    '<button id="map-save" type="button">Validate and save mappings</button>'
                    "</div><details><summary>Source configuration (advanced)</summary>"
                    '<button id="map-template" type="button">'
                    "Load example mappings as draft</button>"
                    '<p class="mapper-help">Select an OCP section to configure its primary '
                    "source table and confirmed joins. Changes stay a draft until saved.</p>"
                    "<label>Source table for the selected OCP section"
                    '<select id="map-base"></select></label>'
                    '<details id="map-relationships"><summary>'
                    "Source connections and joins (advanced)</summary>"
                    '<p class="mapper-help">Foreign keys suggest '
                    "relationships; you must confirm each join. Without a foreign key, select "
                    "the matching columns yourself. Join targets must have a single-column "
                    "primary or unique key to avoid duplicating records.</p>"
                    '<div id="map-joins"></div><div id="map-add-join"></div></details>'
                    '<button id="map-disable" type="button">Remove this resource mapping</button>'
                    "</details><details><summary>Mapping checks</summary>"
                    '<p id="map-contract" class="mapper-help" role="status"></p>'
                    "</details><details><summary>Generated SQL (read-only)</summary>"
                    '<pre id="map-sql" class="mapper-sql">Run a preview to see the generated SQL.'
                    '</pre></details><div id="map-preview-result" class="mapper-preview"></div>'
                    '</div><script src="/admin/assets/mapping.js" defer></script>'
                    + "<details><summary>Advanced JSON mapping</summary>"
                    + form(
                        "mapping",
                        csrf,
                        field("name", "Profile", name)
                        + '<label>Mapping JSON<textarea name="mapping">'
                        + escape(mapping)
                        + "</textarea></label>",
                        "Validate and save mapping",
                    )
                    + "</details>"
                )
            values = (
                {
                    "driver": profile.driver,
                    "host": profile.host or "",
                    "port": str(profile.port or 3306),
                    "database": profile.database_name,
                    "username": profile.username or "",
                    "password_env": profile.password_env_var or "OCP_SOURCE_DB_PASSWORD",
                    "ca": profile.tls_ca_path or "",
                }
                if profile
                else {}
            )
            monitoring = store.get("observium")
            token_env = monitoring.get("token_env", "OCP_OBSERVIUM_API_TOKEN")
            token_status = (
                "Named token variable: available in the running connector's environment."
                if os.getenv(token_env)
                else "Named token variable: missing in the running connector's environment."
            )
            existing_names = {p["name"] for p in snapshot["profiles"]}
            next_name = "database"
            suffix = 2
            while next_name in existing_names:
                next_name = f"database-{suffix}"
                suffix += 1
            password_variable = values.get("password_env", "OCP_SOURCE_DB_PASSWORD")
            managed = bool(os.getenv(password_variable))
            password_control = (
                '<p class="connection-managed">Password managed by server configuration. '
                "Update it in the connector service environment, then restart.</p>"
                if managed
                else '<label>Password<input name="password" type="password" '
                'autocomplete="new-password" maxlength="4096" '
                'placeholder="Leave blank to keep the saved password"></label>'
            )
            return (
                '<p class="setup-intro">Manage your database and monitoring connections. '
                "Passwords are never displayed back in this page.</p>"
                '<div class="connection-list">'
                + " ".join(
                    f'<a href="/admin/sources?profile={quote(p["name"], safe="")}">'
                    f"{escape(p['name'])}</a>"
                    for p in snapshot["profiles"]
                )
                + f'<a href="/admin/sources?profile={quote(next_name, safe="")}">'
                "+ Add database connection</a>"
                '</div><div class="connection-card"><h3>Database connection</h3>'
                "<p><strong>Use a read-only database account.</strong> "
                "Use a dedicated account with SELECT access only to the tables you intend "
                "to share. Do not use root or an administrator.</p>"
                '<form method="post" action="/admin/source">'
                f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
                '<input type="hidden" name="connection_form" value="true">'
                '<div class="connection-fields">'
                + "".join(
                    field(key, label, values.get(key, default))
                    for key, label, default in [
                        ("database", "Database name", ""),
                        ("host", "IP address / hostname", ""),
                        ("port", "Port", "3306"),
                        ("username", "Username", ""),
                    ]
                )
                + (password_control if values.get("driver", "mysql") == "mysql" else "")
                + "</div><details><summary>Advanced settings</summary>"
                + field("name", "Connection name", name)
                + "".join(
                    field(key, label, values.get(key, default))
                    for key, label, default in [
                        ("driver", "Driver: mysql or sqlite", "mysql"),
                        (
                            "password_env",
                            "Password environment variable (optional)",
                            "OCP_SOURCE_DB_PASSWORD" if managed else "",
                        ),
                        ("ca", "MySQL TLS CA path", ""),
                    ]
                )
                + "<p>Remote MySQL requires a trusted TLS CA file. "
                "SQLite connections use an absolute file path as the database name. "
                "Existing plaintext opt-ins are preserved; this page cannot enable them.</p>"
                + '<label>Profile status<select name="enabled">'
                + (
                    '<option value="false">Disabled</option><option value="true">Enabled</option>'
                    if profile is not None and not profile.enabled
                    else '<option value="true">Enabled</option>'
                    '<option value="false">Disabled</option>'
                )
                + "</select></label></details>"
                + '<div class="connection-actions"><button class="secondary" type="submit" '
                'formaction="/admin/test-connection">Test connection</button>'
                '<button type="submit">Save connection</button></div></form></div>'
                + (
                    "<p>Profile disabled. Save it as Enabled before testing or selecting.</p>"
                    if profile is not None and not profile.enabled
                    else ""
                )
                + '<details class="connection-card"><summary>Mapping and activation</summary>'
                "<p>Save the connection first. Review its field mapping before selecting "
                "it for the connector API.</p>"
                f'<p><a href="/admin/mapping?profile={quote(name, safe="")}">'
                "Review database mapping</a></p>"
                + form(
                    "test-source",
                    csrf,
                    f'<input type="hidden" name="name" value="{escape(name)}">',
                    "Validate saved database mapping",
                )
                + form(
                    "activate",
                    csrf,
                    f'<input type="hidden" name="name" value="{escape(name)}">',
                    "Validate and select for API restart",
                )
                + "</details>"
                + '<details class="connection-card"><summary>Monitoring connection '
                "(Observium, optional)</summary><p><strong>The API token must be set in the "
                "connector service's environment. This page accepts the variable NAME only, "
                "not the token.</strong> This form never stores an API token.</p>"
                + f"<p>{escape(token_status)}</p>"
                + "<details><summary>How to set the token on the server (Linux)</summary>"
                "<p>For a manual launch, set the variable in the same shell before starting "
                "the connector. Example using the default variable name:</p>"
                "<pre>export OCP_OBSERVIUM_API_TOKEN='&lt;your-read-only-api-token&gt;'</pre>"
                "<p>For systemd or Docker, configure the variable in that service or "
                "container's environment and restart it. Setting it in another terminal "
                "does not change an already-running connector. A .env file is not loaded "
                "automatically by this app; your deployment must load it. Keep tokens out "
                "of source control and protect any environment files.</p></details>"
                + (
                    '<p class="connection-managed">The running API uses environment-configured '
                    "Observium settings, which override saved UI settings. Update that environment "
                    "and restart to change the deployed connection. The test checks those "
                    "effective settings.</p>"
                    if settings.observium_base_url
                    else ""
                )
                + form(
                    "observium",
                    csrf,
                    field(
                        "url",
                        "Observium HTTPS URL",
                        monitoring.get("url", settings.observium_base_url or ""),
                    )
                    + field(
                        "token_env",
                        "Token environment-variable NAME (not the API token)",
                        token_env,
                    )
                    + "<p>Enter <code>OCP_OBSERVIUM_API_TOKEN</code> above if you use the "
                    "default name. Save the URL and variable name, then test the effective "
                    "connection. Restart after saving to apply changes to the partner API.</p>"
                    + "<details><summary>Advanced: TLS certificate</summary>"
                    + field(
                        "ca",
                        "Optional CA PEM path",
                        monitoring.get("ca", settings.observium_ca_bundle or ""),
                    )
                    + "</details>",
                    "Save Observium settings (no token stored)",
                )
                + form("test-observium", csrf, "", "Test effective Observium connection")
                + "</details>"
            )
        if section == "partners":
            return (
                portal_policy_content(csrf)
                + company_directory(selected_profile, snapshot["profiles"], csrf)
                + "<h3>Partner access</h3>"
                "<p>Partners query this connector without the portal. Confirm bilateral consent "
                "out of band. Grants with no resource IDs cover ALL resources in that scope.</p>"
                + table(snapshot["partners"])
                + table(snapshot["clients"])
                + table(snapshot["grants"])
                + form(
                    "partner",
                    csrf,
                    field("name", "Partner company")
                    + "".join(
                        f'<label><input type="checkbox" name="scope" value="{s}">'
                        f"{escape(s)}</label>"
                        for s in sorted(SCOPES)
                    )
                    + field("resources", "Allowed opaque resource IDs, comma separated")
                    + '<label><input type="checkbox" name="all_resources" value="yes">'
                    "Explicitly allow all resources in selected scopes</label>"
                    + '<label><input type="checkbox" name="consent" value="yes">'
                    "Both companies approved this relationship</label>",
                    "Create partner and one-time OAuth credential",
                )
                + form(
                    "revoke-partner",
                    csrf,
                    field("partner", "Partner ID to revoke"),
                    "Revoke relationship and credentials",
                )
                + "<h3>Circuit shares</h3>"
                + table(snapshot["shares"])
                + form(
                    "share",
                    csrf,
                    field("partner", "Partner ID")
                    + field("circuit", "Local source circuit ID")
                    + '<label>Detail<select name="visibility"><option>endpoints</option>'
                    "<option>sfn_path</option><option>full_shared_path</option>"
                    "<option>summary</option></select></label>"
                    "<p>Visible circuit ports include current monitoring "
                    "when linked and configured. "
                    "This does not grant access to unrelated devices or ports.</p>",
                    "Share circuit",
                )
                + form(
                    "unshare",
                    csrf,
                    field("partner", "Partner ID") + field("circuit", "Local source circuit ID"),
                    "Revoke circuit share",
                )
            )
        if section == "remote":
            name = selected_profile or "partner"
            remote = store.get("remote:" + name)
            return (
                "<p>Query another company's connector independently of the hosted portal. "
                "They must issue credentials and grants first. "
                "Credentials remain in environment variables; "
                "never paste the secret into this page. Redirects are not followed.</p>"
                + "<p>Saved partner connections: "
                + " ".join(
                    f'<a href="/admin/remote?profile={quote(n, safe="")}">{escape(n)}</a>'
                    for n in store.names("remote:")
                )
                + "</p>"
                + form(
                    "remote",
                    csrf,
                    field("name", "Connection name", name)
                    + field("url", "Partner connector HTTPS URL", remote.get("url", ""))
                    + field("client", "OAuth client ID", remote.get("client", ""))
                    + field(
                        "secret_env",
                        "Client secret environment variable",
                        remote.get("secret_env", "OCP_PARTNER_SECRET"),
                    )
                    + field("scope", "Space-separated scopes", remote.get("scope", "circuits:read"))
                    + field("ca", "Optional trusted CA PEM path", remote.get("ca", "")),
                )
                + form(
                    "remote-test",
                    csrf,
                    field("name", "Connection name", name),
                    "Authenticate and view capabilities",
                )
                + form(
                    "remote-circuit",
                    csrf,
                    field("name", "Connection name", name)
                    + field("circuit", "Partner opaque circuit ID"),
                    "View authorized circuit details",
                )
            )
        if section == "account":
            return (
                "<p>Admin accounts are local, not partner OAuth clients or hosted portal users. "
                "Additional accounts/reset require local CLI operator access.</p>"
                + form(
                    "password",
                    csrf,
                    field("old", "Current password", kind="password")
                    + field("password", "New password (10-256 characters)", kind="password")
                    + field("confirm", "Confirm new password", kind="password"),
                    "Change password and sign out",
                )
            )
        return (
            "<p>Recent events only; source records and credentials are not exported.</p>"
            + "<h3>Observium connection logs</h3>"
            + table(store.recent_events("observium"))
            + "<details><summary>All operational logs</summary>"
            + "<p>Latest 100 events shown; the latest 2,000 are retained locally. "
            "The same events are written to service output. For Linux deployments use "
            "journalctl for your systemd service or docker logs for your container. "
            "Long-term retention is managed by your deployment.</p>"
            + table(store.recent_events())
            + "</details>"
            + "<h3>Administration</h3>"
            + table(store.recent_audit())
            + "<h3>Partner API requests</h3>"
            + table(snapshot["audit"])
        )

    def text(data: FormData, key: str, *, required: bool = True) -> str:
        value = data.get(key, "")
        if not isinstance(value, str):
            raise ValueError("text field required")
        if required and not value.strip():
            raise ValueError(f"{key} is required")
        return value if key in {"password", "old", "confirm"} else value.strip()

    def env_name(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("enter an environment variable NAME, not a secret")
        return value

    def execute(action: str, data: FormData, username: str) -> str:
        if action == "identity":
            name = text(data, "name")
            if len(name) > 120:
                raise ValueError("company name must not exceed 120 characters")
            identity = repository.get_connector_identity()
            repository.put_connector_identity(
                replace_identity(identity, name)
                if identity
                else ConnectorIdentity(
                    organization_id=str(uuid.uuid4()),
                    organization_name=name,
                    connector_id=str(uuid.uuid4()),
                    issuer_url=settings.issuer_url,
                )
            )
        elif action == "pair":
            identity = repository.get_connector_identity()
            if identity is None or not identity.enabled:
                raise ValueError("initialize an enabled company identity first")
            if not settings.portal_url:
                raise ValueError(
                    "Carrier Portal address is not configured. Ask your connector administrator "
                    "to set OCP_PORTAL_URL in the connector environment and restart."
                )
            ca = portal_ca()
            with http_client(ca) as client:
                pair_portal(
                    client=client,
                    portal_url=settings.portal_url,
                    connector_id=identity.connector_id,
                    code=text(data, "code"),
                    connection_file=connection_file,
                    allow_insecure_http=settings.allow_insecure_http,
                )
            store.save("portal", {"ca": ca})
            store.save("heartbeat", {})
            return (
                "<p>Paired successfully. The connector will confirm its connection with a "
                'heartbeat.</p><p><a href="/admin/portal">View Carrier Portal connection</a></p>'
            )
        elif action == "heartbeat":
            if not connection_file.exists():
                raise ValueError("pair the connector first")
            heartbeat()
            return "<p>Heartbeat accepted. Source health and live retrieval were not tested.</p>"
        elif action == "disconnect":
            if connection_file.exists():
                connection_file.unlink()
            store.save("heartbeat", {})
        elif action in {"enable-owner-source", "stop-owner-source"}:
            portal = PortalConnection.load(
                connection_file, allow_insecure_http=settings.allow_insecure_http
            )
            if action == "enable-owner-source":
                if text(data, "consent", required=False) != "yes":
                    raise ValueError("explicit owner approval is required")
                approve_owner_source(
                    repository,
                    portal,
                    metadata_path=settings.database_path,
                    profile_name=text(data, "profile"),
                    approved_by=username,
                    explorer_enabled=text(data, "explorer_consent", required=False) == "yes",
                    layout_enabled=text(data, "layout_consent", required=False) == "yes",
                )
            else:
                repository.stop_portal_owner_source(
                    portal, source_profile_id=text(data, "profile_id")
                )
            store.audit(username, action)
            return (
                "<p>Own-company inventory access updated. Stopped access is denied immediately.</p>"
                '<p><a href="/admin/portal">Back to Carrier Portal connection</a></p>'
            )
        elif action in {"approve-company-policy", "stop-company-policy"}:
            portal = PortalConnection.load(
                connection_file, allow_insecure_http=settings.allow_insecure_http
            )
            identity = repository.get_connector_identity()
            if (
                identity is None
                or not identity.enabled
                or identity.connector_id != portal.connector_id
            ):
                raise ValueError("an enabled paired connector identity is required")
            if action == "stop-company-policy":
                policy_id = text(data, "policy")
                repository.stop_portal_policy(portal, policy_id=policy_id)
            else:
                if text(data, "consent", required=False) != "yes":
                    raise ValueError("explicit owner approval is required")
                policy_id, separator, revision = text(data, "policy").partition("@")
                if not separator:
                    raise ValueError("select a versioned portal policy")
                approve_company_policy(
                    repository,
                    portal,
                    metadata_path=settings.database_path,
                    policy_id=policy_id,
                    revision=int(revision),
                    profile_name=text(data, "profile"),
                    company_id=text(data, "company"),
                    approved_by=username,
                    layout_consent=text(data, "layout_consent", required=False) == "yes",
                )
            store.audit(username, f"{action}:{policy_id}")
            return (
                "<p>Company sharing decision saved. A stopped policy is denied immediately. "
                "The next heartbeat reports the decision to the portal.</p>"
                '<p><a href="/admin/partners">Back to Partners</a></p>'
            )
        elif action in {
            "source",
            "test-connection",
            "mapping",
            "mapping-fields",
            "activate",
            "test-source",
            "preview",
            "schema",
        }:
            name = text(data, "name")
            profile = repository.get_source_database_profile(name)
            if action in {"source", "test-connection"}:
                driver = text(data, "driver")
                enabled = text(data, "enabled", required=False) or (
                    "false" if profile is not None and not profile.enabled else "true"
                )
                if enabled not in {"true", "false"}:
                    raise ValueError("invalid profile status")
                port = int(text(data, "port")) if driver == "mysql" else None
                if port is not None and not 1 <= port <= 65535:
                    raise ValueError("MySQL port must be between 1 and 65535")
                password_env = None
                password = text(data, "password", required=False)
                if len(password) > 4096:
                    raise ValueError("database password must not exceed 4096 characters")
                if driver == "mysql":
                    variable = text(data, "password_env", required=False)
                    password_env = env_name(
                        variable
                        or (profile.password_env_var if profile else "")
                        or "OCP_SOURCE_DB_UI_"
                        + hashlib.sha256(name.encode()).hexdigest()[:24].upper()
                    )
                    if os.getenv(password_env) and password:
                        raise ValueError(
                            "Password is managed by server configuration; update it there."
                        )
                if driver == "sqlite" and not Path(text(data, "database")).is_absolute():
                    raise ValueError("use an absolute SQLite source path")
                if action == "test-connection" or data.get("connection_form") == "true":
                    environment = dict(os.environ)
                    if password and password_env:
                        environment[password_env] = password
                    candidate = configured_source_database(
                        driver=driver,
                        database_name=text(data, "database"),
                        mapping_json=profile.mapping_json
                        if profile
                        else SourceMapping({}).to_json(),
                        environment=environment,
                        metadata_path=settings.database_path,
                        host=text(data, "host", required=False) or None,
                        port=port,
                        username=text(data, "username", required=False) or None,
                        password_env_var=password_env,
                        tls_ca_path=text(data, "ca", required=False) or None,
                        allow_insecure=profile.allow_insecure if profile else False,
                        timeout_seconds=profile.timeout_seconds if profile else 10,
                    )
                    if action == "test-connection":
                        candidate.health_check()
                        return (
                            "<p>Database connection succeeded. Nothing was saved. "
                            "This does not verify read-only permissions or database mapping.</p>"
                            '<p><a href="/admin/sources">Back to connections</a></p>'
                        )
                if password and password_env:
                    save_password(settings.database_path, password_env, password)
                repository.put_source_database_profile(
                    name=name,
                    driver=driver,
                    database_name=text(data, "database"),
                    host=text(data, "host", required=False) or None,
                    port=port,
                    username=text(data, "username", required=False) or None,
                    password_env_var=password_env,
                    tls_ca_path=text(data, "ca", required=False) or None,
                    allow_insecure=profile.allow_insecure if profile else False,
                    timeout_seconds=profile.timeout_seconds if profile else 10,
                    enabled=enabled == "true",
                    mapping_json=profile.mapping_json if profile else SourceMapping({}).to_json(),
                )
                if data.get("connection_form") == "true":
                    return (
                        "<p>Database connection saved. No password is displayed or logged.</p>"
                        f'<p><a href="/admin/sources?profile={quote(name, safe="")}">'
                        "Back to this connection</a></p>"
                        "<p>Review the database mapping and select the connection for the API "
                        "when ready. Restart the connector to apply changes.</p>"
                    )
            elif action in {"mapping", "mapping-fields"}:
                if profile is None:
                    raise ValueError("save a source profile first")
                if action == "mapping-fields":
                    current = SourceMapping.from_json(profile.mapping_json)
                    resources = {}
                    for resource, entry in current.resources.items():
                        table_name = text(data, f"table__{resource}", required=False)
                        if not table_name:
                            continue
                        columns = {
                            logical: column
                            for logical in entry.columns
                            if (
                                column := text(
                                    data, f"column__{resource}__{logical}", required=False
                                )
                            )
                        }
                        resources[resource] = ResourceMapping(table_name, columns, entry.joins)
                    mapping = SourceMapping(resources)
                else:
                    mapping = SourceMapping.from_json(text(data, "mapping"))
                if not mapping.resources or any(
                    "source_id" not in entry.columns for entry in mapping.resources.values()
                ):
                    raise ValueError("map at least one resource, with a source_id for each")
                source = configured_source_database(
                    driver=profile.driver,
                    database_name=profile.database_name,
                    mapping_json=mapping.to_json(),
                    environment=os.environ,
                    metadata_path=settings.database_path,
                    host=profile.host,
                    port=profile.port,
                    username=profile.username,
                    password_env_var=profile.password_env_var,
                    tls_ca_path=profile.tls_ca_path,
                    allow_insecure=profile.allow_insecure,
                    timeout_seconds=profile.timeout_seconds,
                )
                source.validate_mapping()
                save_mapping(repository, profile, mapping.to_json())
            elif action == "schema":
                return table(source_for(repository, name, settings.database_path).describe_schema())
            elif action == "preview":
                rows = source_for(repository, name, settings.database_path).fetch(
                    text(data, "resource"), limit=5
                )
                return table([{k: str(v) for k, v in row.items()} for row in rows])
            else:
                source = source_for(repository, name, settings.database_path)
                source.health_check()
                source.validate_mapping()
                missing = source.missing_fields_for(monitoring=monitoring_enabled())
                warning = (
                    (
                        "<p>Required mappings still missing: "
                        + escape(
                            ", ".join(
                                resource + "." + field
                                for resource, fields in missing.items()
                                for field in fields
                            )
                        )
                        + ".</p>"
                    )
                    if missing
                    else ""
                )
                if action == "activate":
                    store.save("source", {"name": name})
                    return (
                        "<p>Source connection selected. Restart the partner API to apply. "
                        "Explicit OCP_SOURCE_DB_PROFILE takes precedence.</p>"
                    ) + warning
                return (
                    "<p>Connectivity and mapped source columns validated. Use a read-only account; "
                    "SELECT tests do not prove its database permissions.</p>"
                ) + warning
        elif action == "observium":
            if "api_token" in data or data.get("token_source") == "saved":
                raise ValueError(
                    "Observium tokens must be set in the connector service's environment, "
                    "not submitted on this page. Refresh Connections and enter only the "
                    "environment-variable name."
                )
            url = text(data, "url").rstrip("/")
            monitoring_url = urlsplit(url)
            if (
                monitoring_url.scheme != "https"
                or not monitoring_url.hostname
                or monitoring_url.username is not None
                or monitoring_url.password is not None
                or monitoring_url.query
                or monitoring_url.fragment
            ):
                raise ValueError(
                    "Observium requires HTTPS without URL credentials or query parameters"
                )
            env = env_name(text(data, "token_env"))
            if not os.getenv(env):
                log_event(
                    "observium",
                    "configuration.token_missing",
                    "Cannot save monitoring settings: the selected token variable is missing "
                    "in this connector process. Set the service environment and restart.",
                    level=logging.ERROR,
                )
                raise ValueError(
                    "The named token environment variable is missing in the connector service. "
                    "Set the actual API token in that service's environment and restart the "
                    "connector, then save these settings. Do not paste the token into this form."
                )
            ca = text(data, "ca", required=False)
            ssl.create_default_context(cafile=ca or None)
            store.save("observium", {"url": url, "token_env": env, "ca": ca})
            log_event(
                "observium",
                "configuration.saved",
                "Monitoring URL, token-variable name and TLS settings saved. "
                "Restart to apply them to the partner API; no token was stored.",
            )
            return (
                "<p>Saved the URL, token environment-variable name and TLS settings only. "
                "No API token was stored. Restart the connector to apply changes to the "
                'partner API.</p><p><a href="/admin/sources">Back to connections</a></p>'
            )
        elif action == "test-observium":
            log_event(
                "observium",
                "connection.test_started",
                "Testing the effective Observium configuration.",
            )
            if "api_token" in data or "url" in data:
                raise ValueError(
                    "Direct token testing has been removed. Set the token in the connector "
                    "service's environment, restart, and refresh Connections to save and test "
                    "the effective settings."
                )
            from open_carrier_portal.adapters.observium import ObserviumAdapter

            effective = configured_settings(settings, store)
            if not effective.observium_enabled:
                log_event(
                    "observium",
                    "configuration.not_configured",
                    "No effective Observium connection is configured. Save the URL and "
                    "token-variable name on Connections, or configure the deployment environment.",
                    level=logging.ERROR,
                )
                raise ValueError("Observium is not configured")
            adapter = ObserviumAdapter(
                base_url=effective.observium_base_url or "",
                api_token=effective.observium_api_token or "",
                repository=repository,
                ca_bundle=effective.observium_ca_bundle,
                timeout_seconds=effective.observium_timeout_seconds,
            )

            async def check():
                try:
                    await adapter.health_check()
                finally:
                    await adapter.close()

            asyncio.run(check())
            log_event(
                "observium",
                "connection.test_passed",
                "Observium health check passed.",
            )
            return (
                "<p>Observium health check passed.</p>"
                + '<p><a href="/admin/sources">Back to connections</a></p>'
            )
        elif action == "partner":
            scopes = {str(s) for s in data.getlist("scope")}
            if not scopes or not scopes <= SCOPES or text(data, "consent") != "yes":
                raise ValueError("select valid scopes and confirm bilateral approval")
            resources = {
                r.strip() for r in text(data, "resources", required=False).split(",") if r.strip()
            }
            if not resources and text(data, "all_resources", required=False) != "yes":
                raise ValueError("specify allowed resource IDs or explicitly confirm all resources")
            secret = generate_client_secret()
            partner, _, client_id = repository.provision_partner(
                name=text(data, "name"),
                secret_hash=hash_client_secret(secret),
                grants={scope: resources for scope in scopes},
            )
            return (
                "<p class=notice>Copy this credential now and deliver it privately. "
                "It is shown only once.</p>"
                + table([{"partner_id": partner, "client_id": client_id, "client_secret": secret}])
            )
        elif action == "revoke-partner":
            repository.revoke_partner(text(data, "partner"))
        elif action in {"share", "unshare"}:
            partner = text(data, "partner")
            if not any(
                p["partner_id"] == partner and p["state"] == "active"
                for p in repository.administration_snapshot()["partners"]
            ):
                raise ValueError("active partner not found")
            if action == "share":
                resource = repository.share_circuit(
                    partner_id=partner,
                    source_circuit_id=text(data, "circuit"),
                    visibility=text(data, "visibility"),
                    created_by_id=username,
                )
                return (
                    f"<p>Shared resource ID: <code>{escape(resource)}</code>. "
                    "A matching circuits:read grant is also required.</p>"
                )
            if not repository.revoke_circuit_share(
                partner_id=partner, source_circuit_id=text(data, "circuit")
            ):
                raise ValueError("active circuit share not found")
        elif action == "remote":
            scopes = text(data, "scope")
            if not set(scopes.split()) <= SCOPES:
                raise ValueError("unsupported scope")
            ca = text(data, "ca", required=False)
            ssl.create_default_context(cafile=ca or None)
            store.save(
                "remote:" + (text(data, "name", required=False) or "partner"),
                {
                    "url": validate_portal_url(text(data, "url")),
                    "client": text(data, "client"),
                    "secret_env": env_name(text(data, "secret_env")),
                    "scope": scopes,
                    "ca": ca,
                },
            )
        elif action in {"remote-test", "remote-circuit"}:
            remote = store.get("remote:" + (text(data, "name", required=False) or "partner"))
            if not remote:
                raise ValueError("configure a partner connection first")
            secret = os.getenv(remote["secret_env"])
            if not secret:
                raise ValueError("partner secret environment variable is missing")
            with http_client(remote.get("ca", "")) as client:
                response = client.post(
                    remote["url"] + "/oauth/token",
                    auth=(remote["client"], secret),
                    data={"grant_type": "client_credentials", "scope": remote["scope"]},
                )
                response.raise_for_status()
                token = response.json().get("access_token")
                if not isinstance(token, str) or not token:
                    raise ValueError("partner returned an invalid OAuth token")
                if (
                    action == "remote-test"
                    and "observium:devices:read" not in remote["scope"].split()
                ):
                    return (
                        "<p>OAuth authentication succeeded. Capabilities were not requested: "
                        "this connector's capability endpoint requires observium:devices:read. "
                        "Use the authorized circuit lookup for a circuits-only connection.</p>"
                    )
                path = "/v1/capabilities"
                if action == "remote-circuit":
                    circuit = text(data, "circuit")
                    if not re.fullmatch(r"circuit_[a-f0-9]{24}", circuit):
                        raise ValueError("use the opaque circuit ID provided by your partner")
                    path = f"/v1/circuits/{circuit}"
                response = client.get(
                    remote["url"] + path, headers={"Authorization": f"Bearer {token}"}
                )
                response.raise_for_status()
                return f"<pre>{escape(json.dumps(response.json(), indent=2)[:50000])}</pre>"
        elif action == "readiness":
            checks = []
            if repository.get_connector_identity() is None:
                raise ValueError("company identity is not initialized")
            name = settings.source_database_profile or store.get("source").get("name")
            if name:
                source = source_for(repository, name, settings.database_path)
                source.health_check()
                source.validate_mapping()
                missing = source.missing_fields_for(monitoring=monitoring_enabled())
                if missing:
                    raise ValueError(
                        "Source schema passed; core or enabled sections still need mappings: "
                        + ", ".join(
                            resource + "." + field
                            for resource, fields in missing.items()
                            for field in fields
                        )
                    )
                checks.append("Source connectivity/mapping passed")
            else:
                checks.append("No source selected: circuit retrieval is not ready")
            checks.append(
                "Portal is optional; heartbeat does not enable live portal data retrieval"
            )
            return "<p>" + "</p><p>".join(escape(item) for item in checks) + "</p>"
        else:
            raise ValueError("unknown administration action")
        return "<p>Saved successfully.</p>"

    @app.get("/admin/assets/mapping.js")
    async def mapping_script():
        return Response(
            Path(__file__).with_name("mapping.js").read_text(encoding="utf-8"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/admin/assets/session.js")
    async def session_script():
        return Response(
            Path(__file__).with_name("session.js").read_text(encoding="utf-8"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/admin/session")
    @app.post("/admin/session")
    async def session_activity(request: Request):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        token = request.cookies.get("ocp_admin", "")
        session = store.session(token)
        if not session:
            log_event(
                "admin",
                "session.unavailable",
                "Administrator session is missing, expired or revoked. Sign in to continue.",
                level=logging.WARNING,
            )
            return JSONResponse({"error": "Sign in to continue."}, status_code=401, headers=headers)
        if request.method == "POST":
            origin = request.headers.get("origin")
            supplied = request.headers.get("X-CSRF-Token", "")
            if origin not in {None, base_url} or not secrets.compare_digest(
                supplied, session["csrf"]
            ):
                log_event(
                    "admin",
                    "session.verification_failed",
                    "Session renewal origin or CSRF verification was rejected.",
                    level=logging.WARNING,
                )
                return JSONResponse(
                    {"error": "Session verification failed. Sign in again."},
                    status_code=403,
                    headers=headers,
                )
            request.state.admin_activity = token
        return JSONResponse(
            {
                "csrf": session["csrf"],
                "username": session["username"],
                "idle_timeout_seconds": ADMIN_SESSION_IDLE_SECONDS,
            },
            headers=headers,
        )

    @app.get("/admin/mapping/data")
    async def mapping_data(request: Request):
        if not store.session(request.cookies.get("ocp_admin", "")):
            return JSONResponse({"error": "Sign in to load database schema."}, status_code=401)
        request.state.admin_activity = request.cookies["ocp_admin"]
        name = request.query_params.get("profile", "")

        def discover():
            source = source_for(repository, name, settings.database_path)
            profile = repository.get_source_database_profile(name)
            assert profile is not None
            return {
                "columns": source.describe_schema(),
                "relationships": source.describe_relationships(),
                "mapping": json.loads(SourceMapping.from_json(profile.mapping_json).to_json()),
                "fields": {
                    resource: list(entry.fields) for resource, entry in SOURCE_RESOURCES.items()
                },
                "groups": MAPPING_GROUPS,
                "target_tables": OCP_TABLES,
                "basic_fields": BASIC_FIELDS,
                "core_resources": CORE_RESOURCES,
                "monitoring_fields": MONITORING_FIELDS,
                "monitoring_enabled": monitoring_enabled(),
                "required_fields": {
                    resource: list(
                        entry.required
                        + (MONITORING_FIELDS.get(resource, ()) if monitoring_enabled() else ())
                    )
                    for resource, entry in SOURCE_RESOURCES.items()
                },
                "private_fields": {
                    resource: list(entry.private) for resource, entry in SOURCE_RESOURCES.items()
                },
                "hidden_fields": {
                    resource: list(entry.legacy) for resource, entry in SOURCE_RESOURCES.items()
                },
                "resource_labels": {
                    resource: entry.label for resource, entry in SOURCE_RESOURCES.items()
                },
                "template": json.loads(SourceMapping.sharing_defaults().to_json()),
            }

        try:
            return JSONResponse(await run_in_threadpool(discover))
        except (ValueError, SourceDatabaseError, OSError) as exc:
            log_failure("database", "schema.failed", exc)
            return JSONResponse(
                {
                    "error": (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "Cannot load schema. Check the enabled database connection, "
                        "credentials and certificate settings on Connections, "
                        "then reload this page."
                    )
                },
                status_code=400,
            )

    @app.post("/admin/mapping/workspace")
    async def mapping_workspace(request: Request):
        session = store.session(request.cookies.get("ocp_admin", ""))
        if not session:
            return JSONResponse({"error": "Sign in before editing mappings."}, status_code=401)
        if request.headers.get("origin") not in {None, base_url}:
            return JSONResponse({"error": "Invalid origin."}, status_code=403)
        if len(await request.body()) > 131072:
            return JSONResponse({"error": "Mapping is too large."}, status_code=413)
        data = await request.form()
        supplied = data.get("csrf")
        if not isinstance(supplied, str) or not secrets.compare_digest(supplied, session["csrf"]):
            return JSONResponse({"error": "Form expired. Reload and sign in."}, status_code=403)
        request.state.admin_activity = request.cookies["ocp_admin"]

        def apply():
            name = text(data, "name")
            mapping = SourceMapping.from_json(text(data, "mapping"))
            if not mapping.resources or any(
                "source_id" not in entry.columns for entry in mapping.resources.values()
            ):
                raise ValueError(
                    "Map at least one resource and a source_id for each enabled resource."
                )
            source = source_for(repository, name, settings.database_path, mapping)
            action = text(data, "operation")
            if action == "save":
                source.validate_mapping()
                profile = repository.get_source_database_profile(name)
                assert profile is not None
                save_mapping(repository, profile, mapping.to_json())
                return {
                    "message": "Mappings validated and saved. Restart the connector to apply "
                    "them to the API. Source data was not changed.",
                    "missing_fields": mapping.missing_fields_for(monitoring=monitoring_enabled()),
                }
            if action != "preview":
                raise ValueError("Unknown mapping operation.")
            resource = text(data, "resource")
            if resource not in mapping.resources:
                raise ValueError("Select a resource with at least its source_id mapped.")
            rows = source.fetch(resource, limit=5)
            return {
                "message": (
                    f"Preview returned {len(rows)} mapped rows. Preview does not save changes."
                ),
                "rows": [
                    {k: None if v is None else str(v) for k, v in row.items()} for row in rows
                ],
                "sql": source.preview_sql(resource),
            }

        try:
            result = await run_in_threadpool(apply)
        except (ValueError, SourceDatabaseError) as exc:
            log_failure("database", "mapping.failed", exc)
            store.audit(session["username"], "mapping.workspace.failed")
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            log_failure("database", "mapping.failed", exc)
            store.audit(session["username"], "mapping.workspace.failed")
            return JSONResponse(
                {"error": "Cannot read source credentials or certificate files."}, status_code=400
            )
        store.audit(session["username"], "mapping.workspace." + str(data.get("operation")))
        log_event("database", "mapping.completed", "Mapping preview or save completed.")
        return JSONResponse(result)

    @app.get("/")
    async def root():
        return RedirectResponse("/admin/setup", status_code=303)

    @app.get("/")
    @app.get("/admin")
    async def admin_home():
        return RedirectResponse("/admin/setup", status_code=303)

    @app.get("/admin/{section}")
    async def admin_page(section: str, request: Request):
        if section == "login":
            csrf = secrets.token_urlsafe(32)
            response = page(
                "Sign in",
                form(
                    "login",
                    csrf,
                    field("username", "Administrator")
                    + field("password", "Password", kind="password"),
                    "Sign in",
                ),
                csrf,
                signed_in=False,
            )
            response.set_cookie(
                "ocp_admin_login",
                csrf,
                httponly=True,
                samesite="strict",
                secure=parsed.scheme == "https",
                max_age=600,
            )
            return response
        session = store.session(request.cookies.get("ocp_admin", ""))
        if not session:
            return RedirectResponse("/admin/login", status_code=303)
        if section not in SECTIONS:
            return page(
                "Not found",
                "<p>Unknown page.</p>",
                session["csrf"],
                status=404,
                username=session["username"],
            )
        request.state.admin_activity = request.cookies["ocp_admin"]
        return page(
            "Connect to Carrier Portal"
            if section == "portal"
            else SECTION_LABELS.get(section, section.title()),
            await run_in_threadpool(
                render, section, session["csrf"], request.query_params.get("profile", "")
            ),
            session["csrf"],
            username=session["username"],
        )

    @app.post("/admin/{action}")
    async def admin_action(action: str, request: Request):
        origin = request.headers.get("origin")
        if origin and origin != base_url:
            log_event(
                "admin",
                "verification.origin_rejected",
                "Admin request origin was rejected.",
                level=logging.WARNING,
            )
            return Response("Invalid origin", status_code=403)
        if len(await request.body()) > 131072:
            return Response("Form too large", status_code=413)
        data = await request.form()
        csrf = data.get("csrf", "")
        session = store.session(request.cookies.get("ocp_admin", ""))
        expected = (
            request.cookies.get("ocp_admin_login", "")
            if action == "login"
            else (session["csrf"] if session else "")
        )
        if not expected or not isinstance(csrf, str) or not secrets.compare_digest(expected, csrf):
            log_event(
                "admin",
                "verification.session_or_csrf_failed",
                "Admin form session or CSRF verification failed. Sign in and resume or "
                "refresh the form; no connection attempt was made.",
                level=logging.WARNING,
            )
            if action in {"observium", "test-observium"}:
                log_event(
                    "observium",
                    "connection.blocked_by_admin_session",
                    "The monitoring action was blocked by expired or invalid admin form "
                    "verification. Sign in again; Observium was not contacted.",
                    level=logging.WARNING,
                )
            return Response("Form expired or unauthorized. Reload and sign in.", status_code=403)
        if session is not None and action != "login":
            request.state.admin_activity = request.cookies["ocp_admin"]
        try:
            if action == "login":
                result = await run_in_threadpool(
                    store.login,
                    text(data, "username"),
                    text(data, "password"),
                    request.client.host if request.client else "unknown",
                )
                if not result:
                    log_event(
                        "admin",
                        "login.failed",
                        "Administrator authentication failed.",
                        level=logging.WARNING,
                    )
                    return page(
                        "Sign in failed",
                        '<p>Incorrect credentials. <a href="/admin/login">Try again</a>.</p>',
                        "",
                        status=401,
                        signed_in=False,
                    )
                response = RedirectResponse("/admin/setup", status_code=303)
                set_session_cookie(response, result[0])
                response.delete_cookie("ocp_admin_login")
                log_event("admin", "login.succeeded", "Administrator authenticated.")
                return response
            if session is None:
                return Response("Sign in required", status_code=403)
            if action == "logout":
                store.logout(request.cookies.get("ocp_admin", ""))
                response = RedirectResponse("/admin/login", status_code=303)
                response.delete_cookie("ocp_admin")
                log_event("admin", "logout.completed", "Administrator signed out.")
                return response
            if action == "password":
                with store.connection() as db:
                    from open_carrier_portal.security import verify_client_secret

                    account = db.execute(
                        "SELECT password_hash FROM local_admins WHERE username = ?",
                        (session["username"],),
                    ).fetchone()
                if not account or not verify_client_secret(
                    text(data, "old"), account["password_hash"]
                ):
                    raise ValueError("current password is incorrect")
                if text(data, "password") != text(data, "confirm"):
                    raise ValueError("passwords do not match")
                store.set_password(session["username"], text(data, "password"), reset=True)
                log_event(
                    "admin",
                    "password.changed",
                    "Administrator password changed; sessions revoked.",
                )
                return RedirectResponse("/admin/login", status_code=303)
            content = await run_in_threadpool(execute, action, data, session["username"])
            store.audit(session["username"], action + ".success")
            log_event(
                "admin",
                "action.completed",
                "Administrator action completed.",
                action=action,
            )
            return page(
                "Result",
                content + '<p><a href="/admin/setup">Back to setup</a></p>',
                expected,
                username=session["username"],
            )
        except (ValueError, ConnectorError, SourceDatabaseError) as exc:
            log_failure("admin", "action.failed", exc)
            store.audit(session["username"] if session else "anonymous", action + ".failed")
            return page(
                "Action failed",
                f"<p>{escape(str(exc))}</p>",
                expected,
                status=400,
                signed_in=session is not None,
                username=session["username"] if session else "",
            )
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            log_failure("admin", "action.failed", exc)
            store.audit(session["username"] if session else "anonymous", action + ".failed")
            _, explanation = failure_reason(exc)
            return page(
                "Connection failed",
                f"<p>{escape(explanation)}</p>"
                "<p>Check server paths, credentials, endpoint, trust and network. "
                "No TLS bypass is permitted. Disconnect an existing pairing file first.</p>",
                expected,
                status=400,
                signed_in=session is not None,
                username=session["username"] if session else "",
            )

    install_request_logging(app, store)
    return app


def replace_identity(identity: ConnectorIdentity, name: str) -> ConnectorIdentity:
    return identity.model_copy(update={"organization_name": name})


def save_mapping(repository: Repository, profile: SourceDatabaseProfile, mapping: str) -> None:
    repository.put_source_database_profile(
        name=profile.name,
        driver=profile.driver,
        host=profile.host,
        port=profile.port,
        database_name=profile.database_name,
        username=profile.username,
        password_env_var=profile.password_env_var,
        tls_ca_path=profile.tls_ca_path,
        allow_insecure=profile.allow_insecure,
        timeout_seconds=profile.timeout_seconds,
        mapping_json=mapping,
        enabled=profile.enabled,
    )
