from __future__ import annotations

import html
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Scope

ACTION_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
INTERACTION_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
ACTION_QUERY = "__ocp_action"
INTERACTION_QUERY = "__ocp_interaction"


@dataclass(frozen=True)
class Interaction:
    action_id: str
    interaction_id: str
    request_id: str = field(default_factory=lambda: str(uuid4()))
    explicit: bool = False
    scope: Scope | None = field(default=None, compare=False, repr=False)

    def headers(self) -> dict[str, str]:
        return {
            "X-Action-ID": self.action_id,
            "X-Interaction-ID": self.interaction_id,
        }


_current: ContextVar[Interaction | None] = ContextVar("ui_interaction", default=None)


def current_interaction() -> Interaction | None:
    return _current.get()


@contextmanager
def interaction_context(value: Interaction) -> Generator[Interaction]:
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


def validate_filters(action_id: str = "", interaction_id: str = "") -> None:
    if action_id and not re.fullmatch(ACTION_PATTERN, action_id):
        raise ValueError("Invalid action ID.")
    if interaction_id and not re.fullmatch(INTERACTION_PATTERN, interaction_id):
        raise ValueError("Invalid interaction ID; use a full UUID from the logs.")


def interaction_from_headers(headers: Mapping[str, str], *, default: str) -> Interaction:
    action = headers.get("x-action-id", "")
    interaction = headers.get("x-interaction-id", "")
    validate_filters(action, interaction)
    if bool(action) != bool(interaction):
        raise ValueError("Action ID and interaction ID must be supplied together.")
    return Interaction(
        action or default, interaction or str(uuid4()), explicit=bool(action)
    )


def request_interaction(request: Request, *, default: str) -> Interaction:
    headers = dict(request.headers)
    action = request.query_params.get(ACTION_QUERY, "")
    interaction = request.query_params.get(INTERACTION_QUERY, "")
    if action or interaction:
        if (
            headers.get("x-action-id", action) != action
            or headers.get("x-interaction-id", interaction) != interaction
        ):
            raise ValueError("Conflicting interaction identifiers.")
        headers.update({"x-action-id": action, "x-interaction-id": interaction})
    value = interaction_from_headers(headers, default=default)
    return Interaction(
        value.action_id, value.interaction_id, value.request_id, value.explicit, request.scope
    )


def interaction_meta(prefix: str) -> str:
    context = current_interaction()
    if context is None:
        return ""
    route = getattr((context.scope or {}).get("route"), "path", "page")
    route = (context.scope or {}).get("interaction_page", route)
    page_id = prefix + "." + re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-")[:70]
    return "".join(
        f'<meta name="{name}" content="{html.escape(value, quote=True)}">'
        for name, value in (
            ("ocp-action-id", context.action_id),
            ("ocp-interaction-id", context.interaction_id),
            ("ocp-page-id", page_id),
        )
    )


def attach_interaction(response: Response, request: Request, context: Interaction) -> None:
    response.headers.setdefault("X-Request-ID", context.request_id)
    for key, value in context.headers().items():
        response.headers.setdefault(key, value)
    location = response.headers.get("location")
    if not context.explicit or not location or response.status_code not in {
        301, 302, 303, 307, 308,
    }:
        return
    target = urlsplit(location)
    if target.scheme not in {"", request.url.scheme} or target.netloc not in {
        "", request.url.netloc,
    }:
        return
    parameters = [
        (key, value) for key, value in parse_qsl(target.query, keep_blank_values=True)
        if key not in {ACTION_QUERY, INTERACTION_QUERY}
    ]
    parameters += [
        (ACTION_QUERY, context.action_id), (INTERACTION_QUERY, context.interaction_id)
    ]
    response.headers["location"] = urlunsplit(target._replace(query=urlencode(parameters)))


def logs_markup(
    rows: list[dict[str, str]], *, base: str, application: str,
    action_id: str = "", interaction_id: str = "", offset: int = 0,
) -> str:
    escape = html.escape
    content = (
        f"<p>{escape(application)} logs only. The latest 2,000 events are retained in this "
        "application's database; service output follows your deployment's retention.</p>"
        "<p>Action ID identifies a control. Interaction ID identifies one press and its "
        "requests. IDs are diagnostic metadata, not proof of identity or authorization.</p>"
        f'<form method="get" action="{base}" data-action-id="{application.lower()}.logs.filter">'
        '<label>Action ID<input name="action_id" maxlength="128" '
        f'value="{escape(action_id, quote=True)}"></label>'
        '<label>Interaction ID<input name="interaction_id" maxlength="36" '
        f'value="{escape(interaction_id, quote=True)}"></label>'
        f'<button type="submit" data-action-id="{application.lower()}.logs.filter">'
        "Filter logs</button></form>"
        f'<p><a href="{base}">Clear filters / newest events</a></p>'
    )
    if not rows:
        return content + "<p>No matching retained events.</p>"
    columns = (
        ("UTC", "level")
        + (("component", "message") if any("component" in row for row in rows) else ())
        + ("event", "action_id", "interaction_id", "reference", "details")
    )
    content += '<div class="data-table scroll"><table><thead><tr>' + "".join(
        f"<th>{escape(name.replace('_', ' ').title())}</th>" for name in columns
    ) + "</tr></thead><tbody>"
    for row in rows[:100]:
        content += "<tr>"
        for column in columns:
            value = row.get(column, "")
            cell = escape(value)
            if column in {"action_id", "interaction_id"} and value:
                target = base + "?" + urlencode({column: value})
                cell = f'<a href="{escape(target, quote=True)}"><code>{cell}</code></a>'
            content += f"<td>{cell}</td>"
        content += "</tr>"
    content += "</tbody></table></div>"
    for label, position in (("Newer", offset - 100), ("Older", offset + 100)):
        if position < 0 or position > 1900 or (label == "Older" and len(rows) <= 100):
            continue
        target = base + "?" + urlencode({
            "action_id": action_id, "interaction_id": interaction_id, "offset": position,
        })
        content += f'<p><a href="{escape(target, quote=True)}">{label} events</a></p>'
    return content
