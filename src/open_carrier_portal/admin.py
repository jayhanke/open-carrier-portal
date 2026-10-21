from __future__ import annotations

import asyncio
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
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData
from starlette.middleware.trustedhost import TrustedHostMiddleware

from open_carrier_portal.admin_store import AdminStore
from open_carrier_portal.config import Settings
from open_carrier_portal.errors import ConnectorError, SourceDatabaseError
from open_carrier_portal.models import ConnectorIdentity
from open_carrier_portal.portal_agent import (
    DEFAULT_CONNECTION_FILE,
    PortalConnection,
    pair_portal,
    send_heartbeat,
    validate_portal_url,
)
from open_carrier_portal.repository import Repository, SourceDatabaseProfile
from open_carrier_portal.security import ALLOWED_SCOPES, generate_client_secret, hash_client_secret
from open_carrier_portal.source_database import (
    ResourceMapping,
    SourceMapping,
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
logger = logging.getLogger(__name__)


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
            raise ValueError("configured Observium token environment variable is missing")
        settings = replace(
            settings,
            observium_base_url=monitoring["url"],
            observium_api_token=token,
            observium_ca_bundle=monitoring.get("ca") or None,
        )
    return settings


def source_for(repository: Repository, name: str):
    profile = repository.get_source_database_profile(name)
    if profile is None or not profile.enabled:
        raise ValueError("enabled source profile not found")
    return configured_source_database(
        driver=profile.driver,
        host=profile.host,
        port=profile.port,
        database_name=profile.database_name,
        username=profile.username,
        password_env_var=profile.password_env_var,
        tls_ca_path=profile.tls_ca_path,
        allow_insecure=profile.allow_insecure,
        timeout_seconds=profile.timeout_seconds,
        mapping_json=profile.mapping_json,
        environment=os.environ,
    )


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
    base_url = validate_portal_url(base_url)
    parsed = urlsplit(base_url)
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()
    store = AdminStore(settings.database_path)
    store.initialize()

    def heartbeat() -> None:
        if not connection_file.exists():
            return
        connection = PortalConnection.load(connection_file)
        identity = repository.get_connector_identity()
        if (
            identity is None
            or not identity.enabled
            or identity.connector_id != connection.connector_id
        ):
            raise ValueError("portal connection does not match enabled connector identity")
        with http_client(store.get("portal").get("ca", "")) as client:
            send_heartbeat(client, connection)
        store.save("heartbeat", {"seen": str(time.time())})

    async def agent_loop() -> None:
        while True:
            try:
                await run_in_threadpool(heartbeat)
            except ValueError, OSError, httpx.HTTPError:
                logger.warning(
                    "Portal heartbeat failed; check pairing, certificate trust and connectivity"
                )
                store.save(
                    "heartbeat",
                    {"error": "Heartbeat failed; test or reconnect on the Portal page."},
                )
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(agent_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Carrier Connector Administration", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed.hostname])
    app.state.repository = repository
    app.state.admin_store = store

    def page(title: str, content: str, csrf: str, *, status: int = 200, signed_in: bool = True):
        if signed_in and configuration_error:
            content = f'<p class="notice">{escape(configuration_error)}</p>' + content
        nav = (
            "<nav>"
            + "".join(f'<a href="/admin/{s}">{escape(s.title())}</a>' for s in SECTIONS)
            + form("logout", csrf, "", "Sign out")
            + "</nav>"
            if signed_in
            else ""
        )
        return HTMLResponse(
            f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} | Connector administration</title>
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
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}small{{color:#475569}}
</style><main><h1>Carrier connector</h1><p>Local administration. Hosted portal is optional.</p>
{nav}<section><h2>{escape(title)}</h2>{content}</section></main></html>""",
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
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

    def render(section: str, csrf: str, selected_profile: str = "") -> str:
        identity = repository.get_connector_identity()
        snapshot = repository.administration_snapshot()
        if section == "setup":
            source = settings.source_database_profile or store.get("source").get("name", "")
            portal = store.get("heartbeat")
            connected = bool(portal.get("seen") and time.time() - float(portal["seen"]) < 90)
            return (
                "<p>Follow these steps. Direct sharing does not require the hosted portal.</p><ol>"
                "<li>Local administrator: configured</li>"
                '<li><a href="/admin/identity">Company identity</a>: '
                f"{'configured' if identity else 'required'}</li>"
                '<li><a href="/admin/portal">Optional portal</a>: '
                f"{'recent heartbeat' if connected else 'not connected'}</li>"
                f'<li><a href="/admin/sources">Sources</a>: {escape(source or "not selected")}</li>'
                '<li><a href="/admin/mapping">Validate mappings and preview data</a></li>'
                '<li><a href="/admin/partners">Review direct sharing permissions</a></li></ol>'
                "<p class=notice>Pairing/heartbeats do not enable live portal retrieval. "
                "Source and Observium changes require a connector service restart. "
                "A configured source is not necessarily healthy: run its validation.</p>"
                + form("readiness", csrf, "", "Run readiness checks")
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
            return (
                f"<p>Paired locally: {paired}</p><p>{escape(store.get('heartbeat'))}</p>"
                "<p>Use an HTTPS portal URL. For an internal CA, give its PEM path on this server. "
                "Certificate verification cannot be disabled. "
                "Get a pairing code from your company admin's portal.</p>"
                + form(
                    "pair",
                    csrf,
                    field("url", "Portal HTTPS URL")
                    + field("code", "One-time pairing code", kind="password")
                    + field("ca", "Optional trusted CA PEM path"),
                    "Pair connector",
                )
                + form("heartbeat", csrf, "", "Test heartbeat now")
                + form(
                    "disconnect",
                    csrf,
                    "<p>Disconnect deletes this server's saved credential. "
                    "Also revoke it in the hosted portal.</p>",
                    "Disconnect locally",
                )
            )
        if section in {"sources", "mapping"}:
            name = (
                selected_profile
                or settings.source_database_profile
                or store.get("source").get("name", "nettool")
            )
            profile = repository.get_source_database_profile(name)
            if section == "mapping":
                mapping = (
                    profile.mapping_json if profile else SourceMapping.nettool_defaults().to_json()
                )
                mapped = SourceMapping.from_json(mapping)
                mapping_fields = "".join(
                    "<details><summary>"
                    + escape(resource)
                    + "</summary>"
                    + field(f"table__{resource}", "Source table", entry.table)
                    + "".join(
                        field(f"column__{resource}__{logical}", f"Column for {logical}", column)
                        for logical, column in entry.columns.items()
                    )
                    + "</details>"
                    for resource, entry in mapped.resources.items()
                )
                return (
                    "<p>Mappings use table/column identifiers, never SQL. "
                    "Each resource maps logical fields to columns. source_id identifies records; "
                    "layout IDs link circuits to ordered items. TableName/TablePK identify "
                    "Port, Patch "
                    "or PW items. Preview is limited to five mapped rows.</p>"
                    "<p>Use the guided fields below. A blank table removes that resource; "
                    "a blank column removes that field. Every enabled resource needs source_id. "
                    "Validation checks tables and columns, not complete referential integrity.</p>"
                    + form(
                        "mapping-fields",
                        csrf,
                        field("name", "Profile", name) + mapping_fields,
                        "Validate and save field mappings",
                    )
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
                    + form(
                        "schema",
                        csrf,
                        field("name", "Profile", name),
                        "Discover table/column names",
                    )
                    + form(
                        "preview",
                        csrf,
                        field("name", "Profile", name)
                        + field("resource", "Mapped resource (for example circuits)", "circuits"),
                        "Preview 5 rows",
                    )
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
            return (
                table(snapshot["profiles"])
                + "<p>Use a dedicated read-only source account. "
                + "<p>Edit profile: "
                + " ".join(
                    f'<a href="/admin/sources?profile={quote(p["name"], safe="")}">'
                    f"{escape(p['name'])}</a> "
                    f'<a href="/admin/mapping?profile={quote(p["name"], safe="")}">Mapping</a>'
                    for p in snapshot["profiles"]
                )
                + "</p>"
                + "<p>Use a dedicated read-only source account. "
                "Enter the NAME of its password environment variable, not its value. "
                "Existing CLI profiles are reused. Remote MySQL requires TLS; existing CLI "
                "insecure opt-in is preserved but cannot be newly enabled here.</p>"
                + form(
                    "source",
                    csrf,
                    field("name", "Profile name", name)
                    + "".join(
                        field(key, label, values.get(key, default))
                        for key, label, default in [
                            ("driver", "Driver: mysql or sqlite", "mysql"),
                            ("host", "MySQL host", ""),
                            ("port", "MySQL port", "3306"),
                            ("database", "Database name or absolute SQLite path", ""),
                            ("username", "Read-only username", ""),
                            (
                                "password_env",
                                "Password environment variable",
                                "OCP_SOURCE_DB_PASSWORD",
                            ),
                            ("ca", "MySQL TLS CA path", ""),
                        ]
                    )
                    + '<label>Profile status<select name="enabled">'
                    + (
                        '<option value="false">Disabled</option>'
                        '<option value="true">Enabled</option>'
                        if profile is not None and not profile.enabled
                        else '<option value="true">Enabled</option>'
                        '<option value="false">Disabled</option>'
                    )
                    + "</select></label>",
                    "Save source profile",
                )
                + (
                    "<p>Profile disabled. Save it as Enabled before testing or selecting.</p>"
                    if profile is not None and not profile.enabled
                    else ""
                )
                + form(
                    "test-source",
                    csrf,
                    field("name", "Profile to test", name),
                    "Test connectivity and mapping",
                )
                + form(
                    "activate",
                    csrf,
                    field("name", "Profile to activate", name),
                    "Validate and select for API restart",
                )
                + "<h3>Observium (optional)</h3><p>Token stays in this server's environment. "
                "Environment-configured Observium settings override saved UI settings.</p>"
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
                        "API token environment variable",
                        monitoring.get("token_env", "OCP_OBSERVIUM_API_TOKEN"),
                    )
                    + field(
                        "ca",
                        "Optional CA PEM path",
                        monitoring.get("ca", settings.observium_ca_bundle or ""),
                    ),
                )
                + form("test-observium", csrf, "", "Test effective Observium connection")
            )
        if section == "partners":
            return (
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
                    "<option>sfn_path</option><option>full_shared_path</option></select></label>",
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
                    "View authorized circuit path",
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
            ca = text(data, "ca", required=False)
            with http_client(ca) as client:
                pair_portal(
                    client=client,
                    portal_url=text(data, "url"),
                    connector_id=identity.connector_id,
                    code=text(data, "code"),
                    connection_file=connection_file,
                )
            store.save("portal", {"ca": ca})
            store.save("heartbeat", {})
            return "<p>Paired. The connector sends heartbeats every 30 seconds while running.</p>"
        elif action == "heartbeat":
            if not connection_file.exists():
                raise ValueError("pair the connector first")
            heartbeat()
            return "<p>Heartbeat accepted. Source health and live retrieval were not tested.</p>"
        elif action == "disconnect":
            if connection_file.exists():
                connection_file.unlink()
            store.save("heartbeat", {})
        elif action in {
            "source",
            "mapping",
            "mapping-fields",
            "activate",
            "test-source",
            "preview",
            "schema",
        }:
            name = text(data, "name")
            profile = repository.get_source_database_profile(name)
            if action == "source":
                driver = text(data, "driver")
                enabled = text(data, "enabled", required=False) or (
                    "false" if profile is not None and not profile.enabled else "true"
                )
                if enabled not in {"true", "false"}:
                    raise ValueError("invalid profile status")
                port = int(text(data, "port")) if driver == "mysql" else None
                if port is not None and not 1 <= port <= 65535:
                    raise ValueError("MySQL port must be between 1 and 65535")
                password_env = env_name(text(data, "password_env")) if driver == "mysql" else None
                if driver == "sqlite" and not Path(text(data, "database")).is_absolute():
                    raise ValueError("use an absolute SQLite source path")
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
                    mapping_json=profile.mapping_json
                    if profile
                    else SourceMapping.nettool_defaults().to_json(),
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
                        resources[resource] = ResourceMapping(table_name, columns)
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
                return table(source_for(repository, name).describe_schema())
            elif action == "preview":
                rows = source_for(repository, name).fetch(text(data, "resource"), limit=5)
                return table([{k: str(v) for k, v in row.items()} for row in rows])
            else:
                source = source_for(repository, name)
                source.health_check()
                source.validate_mapping()
                if action == "activate":
                    store.save("source", {"name": name})
                    return (
                        "<p>Source validated and selected. Restart the partner API to apply. "
                        "Explicit OCP_SOURCE_DB_PROFILE takes precedence.</p>"
                    )
                return (
                    "<p>Connectivity and mapping validated. Use a read-only account; "
                    "SELECT tests do not prove its database permissions.</p>"
                )
        elif action == "observium":
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
                raise ValueError("token environment variable is missing in the connector service")
            ca = text(data, "ca", required=False)
            ssl.create_default_context(cafile=ca or None)
            store.save("observium", {"url": url, "token_env": env, "ca": ca})
            return "<p>Saved. Restart the partner API service to apply; no secret was stored.</p>"
        elif action == "test-observium":
            from open_carrier_portal.adapters.observium import ObserviumAdapter

            effective = configured_settings(settings, store)
            if not effective.observium_enabled:
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
            return "<p>Observium health check passed.</p>"
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
                    path = f"/v1/circuits/{circuit}/path"
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
                source = source_for(repository, name)
                source.health_check()
                source.validate_mapping()
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
            return page("Not found", "<p>Unknown page.</p>", session["csrf"], status=404)
        return page(
            section.title(),
            await run_in_threadpool(
                render, section, session["csrf"], request.query_params.get("profile", "")
            ),
            session["csrf"],
        )

    @app.post("/admin/{action}")
    async def admin_action(action: str, request: Request):
        origin = request.headers.get("origin")
        if origin and origin != base_url:
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
            return Response("Form expired or unauthorized. Reload and sign in.", status_code=403)
        try:
            if action == "login":
                result = await run_in_threadpool(
                    store.login,
                    text(data, "username"),
                    text(data, "password"),
                    request.client.host if request.client else "unknown",
                )
                if not result:
                    return page(
                        "Sign in failed",
                        '<p>Incorrect credentials. <a href="/admin/login">Try again</a>.</p>',
                        "",
                        status=401,
                        signed_in=False,
                    )
                response = RedirectResponse("/admin/setup", status_code=303)
                response.set_cookie(
                    "ocp_admin",
                    result[0],
                    httponly=True,
                    samesite="strict",
                    secure=parsed.scheme == "https",
                    max_age=3600,
                )
                response.delete_cookie("ocp_admin_login")
                return response
            if session is None:
                return Response("Sign in required", status_code=403)
            if action == "logout":
                store.logout(request.cookies.get("ocp_admin", ""))
                response = RedirectResponse("/admin/login", status_code=303)
                response.delete_cookie("ocp_admin")
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
                return RedirectResponse("/admin/login", status_code=303)
            content = await run_in_threadpool(execute, action, data, session["username"])
            store.audit(session["username"], action + ".success")
            return page(
                "Result", content + '<p><a href="/admin/setup">Back to setup</a></p>', expected
            )
        except (ValueError, ConnectorError, SourceDatabaseError) as exc:
            store.audit(session["username"] if session else "anonymous", action + ".failed")
            return page(
                "Action failed",
                f"<p>{escape(str(exc))}</p>",
                expected,
                status=400,
                signed_in=session is not None,
            )
        except httpx.HTTPError, OSError:
            store.audit(session["username"] if session else "anonymous", action + ".failed")
            return page(
                "Connection failed",
                "<p>Request failed. Check server paths, credentials, endpoint, trust and network. "
                "No TLS bypass is permitted. Disconnect an existing pairing file first.</p>",
                expected,
                status=400,
                signed_in=session is not None,
            )

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
