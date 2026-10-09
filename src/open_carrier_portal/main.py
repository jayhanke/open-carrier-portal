import base64
import binascii
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from open_carrier_portal.adapters.base import MonitoringAdapter
from open_carrier_portal.adapters.observium import ObserviumAdapter
from open_carrier_portal.auth import AuthenticationService, Principal
from open_carrier_portal.config import Settings
from open_carrier_portal.errors import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    IntegrationUnavailableError,
    ResourceNotFoundError,
    SourceDatabaseQueryError,
    SourceDatabaseUnavailableError,
    UpstreamResponseError,
)
from open_carrier_portal.models import (
    Alarm,
    CapabilitySet,
    CircuitPath,
    CircuitPathItem,
    Device,
    ErrorResponse,
    Interface,
    MetricSeries,
    Page,
    TokenResponse,
)
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import CursorCodec, TokenCodec
from open_carrier_portal.source_database import (
    SourceDatabase,
    configured_source_database,
)

bearer_scheme = HTTPBearer(auto_error=False)


def create_app(
    settings: Settings,
    *,
    repository: Repository | None = None,
    adapter: MonitoringAdapter | None = None,
    source_database: SourceDatabase | None = None,
) -> FastAPI:
    repository = repository or Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()
    if source_database is None and settings.source_database_profile:
        profile = repository.get_source_database_profile(settings.source_database_profile)
        if profile is None:
            raise ConfigurationError(
                f"source database profile {settings.source_database_profile!r} does not exist"
            )
        if not profile.enabled:
            raise ConfigurationError(
                f"source database profile {settings.source_database_profile!r} is disabled"
            )
        source_database = configured_source_database(
            driver=profile.driver,
            host=profile.host,
            port=profile.port,
            database_name=profile.database_name,
            username=profile.username,
            password_env_var=profile.password_env_var,
            tls_ca_path=profile.tls_ca_path,
            timeout_seconds=profile.timeout_seconds,
            mapping_json=profile.mapping_json,
            environment=os.environ,
        )
    token_codec = TokenCodec(
        issuer=settings.issuer_url,
        audience=settings.token_audience,
        signing_key=settings.token_signing_key,
        key_id=settings.token_key_id,
        ttl_seconds=settings.access_token_ttl_seconds,
    )
    auth_service = AuthenticationService(repository, token_codec)
    cursor_codec = CursorCodec(settings.token_signing_key)
    if adapter is None and settings.observium_enabled:
        adapter = ObserviumAdapter(
            base_url=settings.observium_base_url or "",
            api_token=settings.observium_api_token or "",
            repository=repository,
            timeout_seconds=settings.observium_timeout_seconds,
            ca_bundle=settings.observium_ca_bundle,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        close = getattr(adapter, "close", None)
        if close is not None:
            await close()

    app = FastAPI(
        title="Open Carrier Portal Connector",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.repository = repository
    app.state.adapter = adapter
    app.state.source_database = source_database
    app.state.auth_service = auth_service

    @app.middleware("http")
    async def correlation_id_middleware(
        request: Request,
        call_next: Callable,
    ):
        supplied = request.headers.get("X-Correlation-ID")
        correlation_id = supplied if supplied and len(supplied) <= 128 else str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    @app.exception_handler(ConnectorError)
    async def connector_error_handler(request: Request, exc: ConnectorError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(exc.retry_after_seconds)
        return JSONResponse(
            status_code=exc.status_code,
            headers=headers,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "correlation_id": request.state.correlation_id,
                    "retry_after_seconds": exc.retry_after_seconds,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        _: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request parameters are invalid",
                    "correlation_id": request.state.correlation_id,
                    "retry_after_seconds": None,
                }
            },
        )

    def current_principal(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer_scheme),
        ],
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AuthenticationError("A Bearer access token is required")
        return auth_service.authenticate(credentials.credentials)

    def require_adapter() -> MonitoringAdapter:
        if adapter is None:
            raise IntegrationUnavailableError("Observium integration is not configured")
        return adapter

    def require_source_database() -> SourceDatabase:
        if source_database is None:
            raise IntegrationUnavailableError("NetTool source database is not configured")
        return source_database

    def authorize(
        request: Request,
        principal: Principal,
        scope: str,
        resource_id: str | None = None,
    ) -> set[str] | None:
        try:
            return repository.authorize(
                client_id=principal.client_id,
                partner_id=principal.partner_id,
                token_scopes=principal.scopes,
                required_scope=scope,
                resource_id=resource_id,
            )
        except ConnectorError:
            repository.record_audit(
                partner_id=principal.partner_id,
                client_id=principal.client_id,
                operation=scope,
                resource_summary=resource_id,
                outcome="denied",
                correlation_id=request.state.correlation_id,
            )
            raise

    def audit_success(
        request: Request,
        principal: Principal,
        scope: str,
        resource_id: str | None = None,
    ) -> None:
        repository.record_audit(
            partner_id=principal.partner_id,
            client_id=principal.client_id,
            operation=scope,
            resource_summary=resource_id,
            outcome="success",
            correlation_id=request.state.correlation_id,
        )

    @app.post(
        "/oauth/token",
        response_model=TokenResponse,
        responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
    )
    async def issue_token(
        request: Request,
        grant_type: Annotated[str, Form()],
        scope: Annotated[str, Form()] = "",
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
    ) -> TokenResponse:
        if grant_type != "client_credentials":
            raise ConnectorError(
                400,
                "unsupported_grant_type",
                "Only client_credentials is supported",
            )
        resolved_client_id, resolved_client_secret = _client_credentials(
            request,
            client_id,
            client_secret,
        )
        token, expires_in, granted_scopes = auth_service.issue_token(
            client_id=resolved_client_id,
            client_secret=resolved_client_secret,
            requested_scopes=set(scope.split()),
        )
        return TokenResponse(
            access_token=token,
            expires_in=expires_in,
            scope=" ".join(sorted(granted_scopes)),
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness() -> dict[str, str]:
        identity = repository.get_connector_identity()
        if identity is None or not identity.enabled or adapter is None:
            raise IntegrationUnavailableError("Connector setup is incomplete")
        await adapter.health_check()
        return {"status": "ready"}

    @app.get("/v1/capabilities", response_model=CapabilitySet)
    async def capabilities(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        monitoring: Annotated[MonitoringAdapter, Depends(require_adapter)],
    ) -> CapabilitySet:
        scope = "observium:devices:read"
        authorize(request, principal, scope)
        result = await monitoring.capabilities()
        audit_success(request, principal, scope)
        return result

    @app.get("/v1/devices", response_model=Page[Device])
    async def list_devices(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        monitoring: Annotated[MonitoringAdapter, Depends(require_adapter)],
        page_size: Annotated[int, Query(ge=1)] = min(50, settings.max_page_size),
        cursor: str | None = None,
    ) -> Page[Device]:
        scope = "observium:devices:read"
        allowed_ids = authorize(request, principal, scope)
        bounded_page_size = min(page_size, settings.max_page_size)
        page = (
            cursor_codec.decode(cursor, partner_id=principal.partner_id, route="devices")
            if cursor
            else 1
        )
        result = await monitoring.list_devices(page=page, page_size=bounded_page_size)
        next_cursor = (
            cursor_codec.encode(
                partner_id=principal.partner_id,
                route="devices",
                page=int(result.next_cursor),
            )
            if result.next_cursor
            else None
        )
        items = (
            result.items
            if allowed_ids is None
            else [item for item in result.items if item.id in allowed_ids]
        )
        audit_success(request, principal, scope)
        return Page(items=items, next_cursor=next_cursor)

    @app.get("/v1/devices/{device_id}", response_model=Device)
    async def get_device(
        device_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        monitoring: Annotated[MonitoringAdapter, Depends(require_adapter)],
    ) -> Device:
        scope = "observium:devices:read"
        try:
            authorize(request, principal, scope, device_id)
        except ConnectorError:
            raise ResourceNotFoundError() from None
        result = await monitoring.get_device(device_id)
        audit_success(request, principal, scope, device_id)
        return result

    @app.get("/v1/interfaces", response_model=Page[Interface])
    async def list_interfaces(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        monitoring: Annotated[MonitoringAdapter, Depends(require_adapter)],
        page_size: Annotated[int, Query(ge=1)] = min(50, settings.max_page_size),
        cursor: str | None = None,
    ) -> Page[Interface]:
        scope = "observium:interfaces:read"
        allowed_ids = authorize(request, principal, scope)
        bounded_page_size = min(page_size, settings.max_page_size)
        page = (
            cursor_codec.decode(cursor, partner_id=principal.partner_id, route="interfaces")
            if cursor
            else 1
        )
        result = await monitoring.list_interfaces(page=page, page_size=bounded_page_size)
        next_cursor = (
            cursor_codec.encode(
                partner_id=principal.partner_id,
                route="interfaces",
                page=int(result.next_cursor),
            )
            if result.next_cursor
            else None
        )
        items = (
            result.items
            if allowed_ids is None
            else [item for item in result.items if item.id in allowed_ids]
        )
        audit_success(request, principal, scope)
        return Page(items=items, next_cursor=next_cursor)

    @app.get("/v1/interfaces/{interface_id}", response_model=Interface)
    async def get_interface(
        interface_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        monitoring: Annotated[MonitoringAdapter, Depends(require_adapter)],
    ) -> Interface:
        scope = "observium:interfaces:read"
        try:
            authorize(request, principal, scope, interface_id)
        except ConnectorError:
            raise ResourceNotFoundError() from None
        result = await monitoring.get_interface(interface_id)
        audit_success(request, principal, scope, interface_id)
        return result

    @app.get("/v1/alarms", response_model=Page[Alarm])
    async def list_alarms(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        monitoring: Annotated[MonitoringAdapter, Depends(require_adapter)],
        page_size: Annotated[int, Query(ge=1)] = min(50, settings.max_page_size),
        cursor: str | None = None,
    ) -> Page[Alarm]:
        scope = "observium:alarms:read"
        allowed_ids = authorize(request, principal, scope)
        bounded_page_size = min(page_size, settings.max_page_size)
        page = (
            cursor_codec.decode(cursor, partner_id=principal.partner_id, route="alarms")
            if cursor
            else 1
        )
        result = await monitoring.list_alarms(page=page, page_size=bounded_page_size)
        next_cursor = (
            cursor_codec.encode(
                partner_id=principal.partner_id,
                route="alarms",
                page=int(result.next_cursor),
            )
            if result.next_cursor
            else None
        )
        items = (
            result.items
            if allowed_ids is None
            else [item for item in result.items if item.id in allowed_ids]
        )
        audit_success(request, principal, scope)
        return Page(items=items, next_cursor=next_cursor)

    @app.get("/v1/metrics", response_model=MetricSeries)
    async def query_metrics(
        request: Request,
        resource_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        step_seconds: Annotated[int, Query(ge=1)],
        principal: Annotated[Principal, Depends(current_principal)],
        monitoring: Annotated[MonitoringAdapter, Depends(require_adapter)],
    ) -> MetricSeries:
        scope = "observium:metrics:read"
        try:
            authorize(request, principal, scope, resource_id)
        except ConnectorError:
            raise ResourceNotFoundError() from None
        duration = int((end - start).total_seconds())
        if duration <= 0 or duration > settings.max_metric_range_seconds:
            raise ConnectorError(400, "invalid_metric_range", "The metric time range is invalid")
        if duration // step_seconds + 1 > settings.max_metric_points:
            raise ConnectorError(400, "too_many_metric_points", "The metric query is too large")
        result = await monitoring.query_metrics(
            resource_id=resource_id,
            metric=metric,
            start=start,
            end=end,
            step_seconds=step_seconds,
        )
        audit_success(request, principal, scope, resource_id)
        return result

    @app.get("/v1/circuits/{circuit_id}/path", response_model=CircuitPath)
    def get_circuit_path(
        circuit_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        source: Annotated[SourceDatabase, Depends(require_source_database)],
    ) -> CircuitPath:
        scope = "circuits:read"
        allowed_circuit_ids = authorize(request, principal, scope)

        def hide_unavailable_circuit() -> None:
            repository.record_audit(
                partner_id=principal.partner_id,
                client_id=principal.client_id,
                operation=scope,
                resource_summary=circuit_id,
                outcome="denied",
                correlation_id=request.state.correlation_id,
            )
            raise ResourceNotFoundError()

        if allowed_circuit_ids is not None and circuit_id not in allowed_circuit_ids:
            hide_unavailable_circuit()
        source_circuit_id = repository.source_resource_id(
            circuit_id,
            "nettool",
            "circuit",
        )
        if source_circuit_id is None:
            hide_unavailable_circuit()
        visibility = repository.circuit_share_visibility(
            partner_id=principal.partner_id,
            source_circuit_id=source_circuit_id,
        )
        if visibility not in {"endpoints", "sfn_path", "full_shared_path"}:
            hide_unavailable_circuit()

        def query_source(
            resource_type: str,
            filters: dict[str, object],
            *,
            order_by: str | None = None,
        ) -> list[dict[str, object]]:
            try:
                return source.fetch_matching(
                    resource_type,
                    filters,
                    order_by=order_by,
                    limit=1000,
                )
            except SourceDatabaseUnavailableError as exc:
                raise IntegrationUnavailableError("NetTool source database is unavailable") from exc
            except (ConfigurationError, SourceDatabaseQueryError) as exc:
                raise UpstreamResponseError(
                    "NetTool source database mapping or query failed"
                ) from exc

        circuit_rows = query_source("circuits", {"source_id": source_circuit_id})
        if not circuit_rows:
            hide_unavailable_circuit()
        circuit = circuit_rows[0]
        layout_id = circuit.get("circuit_layout_id")
        if layout_id is None:
            hide_unavailable_circuit()
        layout_rows = query_source("circuit_layouts", {"source_id": str(layout_id)})
        if not layout_rows:
            raise UpstreamResponseError("The shared circuit references a missing NetTool layout")
        layout = layout_rows[0]
        path_items: list[CircuitPathItem] = []

        def add_port(source_port_id: object, position: int) -> None:
            port_id = str(source_port_id)
            port_rows = query_source("ports", {"source_id": port_id})
            if not port_rows:
                raise UpstreamResponseError(
                    "The shared circuit path references a missing NetTool port"
                )
            port = port_rows[0]
            device_id = port.get("device_id")
            path_items.append(
                CircuitPathItem(
                    position=position,
                    kind="port",
                    resource_id=repository.ocp_resource_id("nettool", "port", port_id),
                    name=None if port.get("name") is None else str(port["name"]),
                    device_id=(
                        None
                        if device_id is None
                        else repository.ocp_resource_id("nettool", "device", str(device_id))
                    ),
                )
            )

        if visibility == "endpoints":
            for position, endpoint in enumerate((layout.get("port_a_id"), layout.get("port_z_id"))):
                if endpoint is not None:
                    add_port(endpoint, position)
        else:
            layout_source_id = str(layout["source_id"])
            layout_items = query_source(
                "circuit_layout_items",
                {"circuit_layout_id": layout_source_id},
                order_by="position",
            )
            for row in layout_items:
                source_id = row.get("item_source_id")
                item_type = str(row.get("item_type", "")).strip().casefold()
                if source_id is None:
                    raise UpstreamResponseError(
                        "A shared NetTool circuit path item has no source reference"
                    )
                position = int(row["position"])
                if item_type == "port":
                    add_port(source_id, position)
                    continue
                if item_type == "patch":
                    resource_type = "patches"
                    kind = "patch"
                elif item_type in {"pw", "pseudowire"}:
                    resource_type = "pseudowires"
                    kind = "pseudowire"
                else:
                    raise UpstreamResponseError(
                        "The shared NetTool circuit path contains an unsupported item type"
                    )
                component_rows = query_source(
                    resource_type,
                    {"source_id": str(source_id)},
                )
                if not component_rows:
                    raise UpstreamResponseError(
                        "The shared circuit path references a missing NetTool component"
                    )
                component = component_rows[0]

                def public_port_id(
                    component_record: dict[str, object],
                    field: str,
                ) -> str | None:
                    value = component_record.get(field)
                    return (
                        None
                        if value is None
                        else repository.ocp_resource_id("nettool", "port", str(value))
                    )

                path_items.append(
                    CircuitPathItem(
                        position=position,
                        kind=kind,
                        resource_id=repository.ocp_resource_id(
                            "nettool",
                            kind,
                            str(source_id),
                        ),
                        patch_type=(
                            None
                            if component.get("patch_type") is None
                            else str(component["patch_type"])
                        ),
                        port_a_id=public_port_id(component, "port_a_id"),
                        port_b_id=public_port_id(component, "port_b_id"),
                    )
                )

        audit_success(request, principal, scope, circuit_id)
        return CircuitPath(
            circuit_id=circuit_id,
            visibility=visibility,
            items=path_items,
        )

    return app


def create_app_from_env() -> FastAPI:
    return create_app(Settings.from_env())


def _client_credentials(
    request: Request,
    client_id: str | None,
    client_secret: str | None,
) -> tuple[str, str]:
    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "basic" or not value:
            raise AuthenticationError()
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
            basic_id, separator, basic_secret = decoded.partition(":")
        except binascii.Error, UnicodeDecodeError:
            raise AuthenticationError() from None
        if not separator or not basic_id or not basic_secret:
            raise AuthenticationError()
        if client_id is not None or client_secret is not None:
            raise AuthenticationError("Use one client authentication method")
        return basic_id, basic_secret
    if not client_id or not client_secret:
        raise AuthenticationError()
    return client_id, client_secret
