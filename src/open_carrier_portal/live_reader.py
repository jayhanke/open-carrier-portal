from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from open_carrier_portal.adapters.base import MonitoringAdapter
from open_carrier_portal.circuits import CircuitReader
from open_carrier_portal.company_access import require_company_circuit
from open_carrier_portal.company_policies import source_fingerprint
from open_carrier_portal.config import Settings
from open_carrier_portal.diagnostics import log_event, log_failure, record_data_issue
from open_carrier_portal.errors import (
    AuthorizationError,
    ConfigurationError,
    ConnectorError,
    ResourceNotFoundError,
    SourceDatabaseError,
    UpstreamResponseError,
)
from open_carrier_portal.live_contract import (
    CircuitEndpoint,
    CircuitPage,
    CircuitSummary,
    InventoryRef,
    LayoutPort,
    LiveCircuit,
    LiveJob,
    LiveLayout,
    LivePathItem,
    LivePort,
    LiveRequest,
    LiveResult,
    MonitoringStatus,
    OwnerExploreRequest,
    OwnerLiveRequest,
    OwnerSourcePage,
    OwnerSourcesRequest,
    UnavailableEndpoint,
)
from open_carrier_portal.models import CircuitPort as SourcePort
from open_carrier_portal.portal_agent import PortalConnection
from open_carrier_portal.repository import Repository
from open_carrier_portal.source_database import configured_profile_database


@asynccontextmanager
async def monitoring_for_live(
    settings: Settings, repository: Repository, *, enabled: bool
) -> AsyncGenerator[MonitoringAdapter | None]:
    if not enabled or not settings.observium_enabled:
        if enabled:
            log_event(
                "observium", "live.monitoring_not_configured",
                "Live circuit monitoring is not configured; check the Observium URL and "
                "token variable under Connections.",
                level=logging.WARNING,
            )
        yield None
        return
    from open_carrier_portal.adapters.observium import ObserviumAdapter

    adapter = ObserviumAdapter(
        base_url=settings.observium_base_url or "",
        api_token=settings.observium_api_token or "",
        repository=repository,
        timeout_seconds=min(settings.observium_timeout_seconds, 10),
        ca_bundle=settings.observium_ca_bundle,
    )
    try:
        yield adapter
    finally:
        await adapter.close()


async def execute_live_request(
    repository: Repository,
    connection: PortalConnection,
    job: LiveJob,
    *,
    metadata_path: Path,
    monitoring: MonitoringAdapter | None = None,
) -> LiveResult:
    outcome = "error"
    try:
        remaining = job.expires_at - time.time()
        if (
            not 0 < remaining <= 30
            or str(job.owner_organization_id) != connection.organization_id
            or str(job.connector_id) != connection.connector_id
        ):
            raise AuthorizationError("live request does not match this paired connector")
        if isinstance(job, OwnerSourcesRequest):
            result = LiveResult(
                request_id=job.request_id,
                sources=OwnerSourcePage(items=repository.list_portal_owner_sources(connection)),
            )
        elif isinstance(job, OwnerExploreRequest):
            from open_carrier_portal.inventory_reader import read_inventory

            result = await asyncio.wait_for(
                run_in_threadpool(read_inventory, repository, connection, job, metadata_path),
                remaining,
            )
        else:
            result = await asyncio.wait_for(
                _read(repository, connection, job, metadata_path, monitoring), remaining
            )
        outcome = "success"
        return result
    except AuthorizationError:
        outcome = "denied"
        return LiveResult(request_id=job.request_id, error="access_denied")
    except ResourceNotFoundError:
        outcome = "denied"
        return LiveResult(request_id=job.request_id, error="not_found")
    except ValidationError:
        log_event(
            "portal",
            "live.invalid_data",
            "Source data does not match the live contract; source values were not logged.",
            level=logging.ERROR,
        )
        return LiveResult(request_id=job.request_id, error="unavailable")
    except (TimeoutError, ConfigurationError, SourceDatabaseError, ConnectorError) as exc:
        log_failure("portal", "live.read_failed", exc)
        return LiveResult(request_id=job.request_id, error="unavailable")
    finally:
        repository.record_audit(
            partner_id=(
                str(job.recipient_organization_id) if isinstance(job, LiveRequest) else None
            ),
            client_id=None,
            operation="portal." + job.operation,
            resource_summary=str(job.request_id),
            outcome=outcome,
            correlation_id=str(job.request_id),
        )


def _project_port(
    port: SourcePort, *, context: bool, issues: list[str] | None
) -> LivePort:
    device = port.device
    if device is None:
        raise UpstreamResponseError("a circuit port has no device")
    rack = device.site
    location = rack.location if rack else None
    statuses: list[MonitoringStatus] = []
    for label, state, observed, fields in (
        (
            "Port", port.monitoring_state, port.monitoring,
            {
                "operational_status", "administrative_status", "optical_rx_dbm",
                "optical_tx_dbm", "last_observed_at", "speed_bps",
            },
        ),
        (
            "Device", device.monitoring_state, device.monitoring,
            {"operational_status", "last_observed_at"},
        ),
    ):
        if observed is not None and state == "available":
            try:
                status = MonitoringStatus(state="available", **observed.model_dump(include=fields))
            except ValidationError:
                if issues is None:
                    raise
                record_data_issue(
                    issues,
                    f"{label} monitoring readings are invalid and are shown as unavailable.",
                    event="circuit.data_unavailable",
                )
                status = MonitoringStatus(state="unavailable")
        else:
            status = MonitoringStatus(
                state="not_linked" if state == "not_linked" else "unavailable"
            )
        statuses.append(status)
    port_status, device_status = statuses

    def display_name(name: str, label: str) -> str:
        if len(name) > 512 and issues is not None:
            record_data_issue(
                issues, f"{label} was shortened to fit the circuit view.",
                event="circuit.data_unavailable",
            )
            return name[:512]
        return name

    return LivePort(
        port_id=port.id,
        port_name=port.name,
        device_id=device.id,
        device_name=device.name,
        rack=(
            InventoryRef(
                resource="racks", id=rack.id,
                name=display_name(rack.name or "Rack", "A rack display name"),
            )
            if context and rack
            else None
        ),
        location=(
            InventoryRef(
                resource="locations",
                id=location.id,
                name=display_name(
                    ", ".join(value for value in (location.city, location.address1) if value)
                    or "Site", "A location display name",
                ),
            )
            if context and location
            else None
        ),
        port_status=port_status,
        device_status=device_status,
    )


async def _read(
    repository: Repository,
    connection: PortalConnection,
    job: LiveRequest | OwnerLiveRequest,
    metadata_path: Path,
    monitoring: MonitoringAdapter | None,
) -> LiveResult:
    def require():
        assert isinstance(job, LiveRequest)
        return repository.require_portal_company_policy(
            connection,
            policy_id=str(job.policy_id),
            revision=job.revision,
            recipient_organization_id=str(job.recipient_organization_id),
        )

    approved = None
    inventory_links = False
    layout_enabled = False
    if isinstance(job, LiveRequest):
        approved = require()
        if job.visibility != approved.visibility:
            raise AuthorizationError("requested circuit view is not locally approved")
        layout_enabled = approved.visibility == "full_shared_path"
        profile = repository.get_source_database_profile_by_id(str(approved.source_profile_id))
        if profile is None or source_fingerprint(profile) != approved.mapping_fingerprint:
            raise AuthorizationError("approved source mapping is no longer current")
        integration = f"portal-profile:{profile.profile_id}:{approved.mapping_fingerprint}"
    else:
        profile = repository.require_portal_owner_source(
            connection, source_profile_id=str(job.source_profile_id), grant_id=str(job.grant_id)
        )
        integration = (
            f"portal-owner:{profile.profile_id}:{source_fingerprint(profile)}:{job.grant_id}"
        )
        for item in repository.list_portal_owner_sources(connection):
            if item.grant_id == job.grant_id:
                inventory_links = item.explorer_enabled
                layout_enabled = item.layout_enabled
                break
    source = configured_profile_database(
        replace(profile, timeout_seconds=min(profile.timeout_seconds, 10)), metadata_path
    )
    if approved is not None:
        companies = await run_in_threadpool(
            source.fetch_matching, "companies", {"source_id": approved.source_company_id}, limit=1
        )
        if len(companies) != 1:
            raise AuthorizationError("approved company is no longer present")
    reader = CircuitReader(
        repository,
        source,
        monitoring,
        integration=integration,
        monitoring_required=not layout_enabled,
        allow_unresolved_path_items=layout_enabled,
    )
    reader.require_view(
        "summary"
        if job.operation == "circuits.list"
        else "full_shared_path"
        if layout_enabled
        else "endpoints"
    )
    if job.operation == "circuits.list":
        if job.query.strip() or job.order != "source":
            rows = await run_in_threadpool(
                source.fetch_explorer_page,
                "circuits",
                filters={"billing_company_id": approved.source_company_id} if approved else None,
                query=job.query,
                order=job.order,
                offset=job.offset,
                limit=job.limit,
            )
        elif approved is not None:
            rows = await run_in_threadpool(
                source.fetch_page,
                "circuits",
                {"billing_company_id": approved.source_company_id},
                offset=job.offset,
                limit=job.limit,
            )
        else:
            rows = await run_in_threadpool(
                source.fetch_inventory_page, "circuits", offset=job.offset, limit=job.limit
            )
        identifiers = [str(row["source_id"]) for row in rows]
        if len(set(identifiers)) != len(identifiers) or any(
            row["source_id"] is None or not str(row["source_id"]).strip() for row in rows
        ):
            raise UpstreamResponseError("circuit identifiers must be unique and nonempty")
        result = LiveResult(
            request_id=job.request_id,
            page=CircuitPage(
                items=[
                    CircuitSummary(
                        id=reader.public_id("circuit", row["source_id"]),
                        name=None if row["name"] is None else str(row["name"]),
                    )
                    for row in rows[: job.limit]
                ],
                next_offset=job.offset + job.limit if len(rows) > job.limit else None,
            ),
        )
    else:
        assert job.circuit_id is not None
        source_id = repository.source_resource_id(job.circuit_id, integration, "circuit")
        if source_id is None:
            raise ResourceNotFoundError()
        filters = {"source_id": source_id}
        if approved is not None:
            filters["billing_company_id"] = approved.source_company_id
        rows = await reader.rows("circuits", filters)
        if not rows:
            raise ResourceNotFoundError()
        if len(rows) != 1:
            raise UpstreamResponseError("circuit identifier resolves to multiple rows")
        details = await reader.details(job.circuit_id, rows[0], "endpoints")
        assert details.path is not None
        endpoints = []
        for position, item in zip(("a", "z"), details.path.items, strict=True):
            port = item.port
            if port is None:
                if not layout_enabled or item.issue != "missing_reference":
                    raise UpstreamResponseError("a circuit endpoint has no port")
                endpoints.append(UnavailableEndpoint(position="a" if position == "a" else "z"))
            else:
                endpoints.append(
                    CircuitEndpoint(
                        position="a" if position == "a" else "z",
                        **_project_port(
                            port, context=inventory_links or layout_enabled,
                            issues=reader.issues if layout_enabled else None,
                        ).model_dump(),
                    )
                )
        layout = None
        if layout_enabled:
            path = await reader.path(job.circuit_id, rows[0], "full_shared_path")
            path_items = []
            for item in path.items:
                ports = []
                for role, value in (
                    ("port", item.port),
                    ("a", item.port_a),
                    ("z", item.port_b),
                    ("a_secondary", item.port_a_secondary),
                    ("z_secondary", item.port_b_secondary),
                ):
                    if value is not None:
                        ports.append(
                            LayoutPort.model_validate(
                                {
                                    **_project_port(
                                        value, context=True, issues=reader.issues
                                    ).model_dump(),
                                    "role": role,
                                }
                            )
                        )
                path_items.append(
                    LivePathItem(
                        position=item.position,
                        kind=item.kind,
                        name=item.name,
                        issue=item.issue,
                        patch_type=str(item.patch_type) if item.patch_type is not None else None,
                        ports=ports,
                    )
                )
            layout = LiveLayout(items=path_items)
        if isinstance(job, LiveRequest):
            await run_in_threadpool(
                require_company_circuit,
                repository,
                connection,
                metadata_path=metadata_path,
                policy_id=str(job.policy_id),
                revision=job.revision,
                recipient_organization_id=str(job.recipient_organization_id),
                circuit_source_id=source_id,
            )
        elif len(await reader.rows("circuits", {"source_id": source_id})) != 1:
            raise ResourceNotFoundError()
        result = LiveResult(
            request_id=job.request_id,
            circuit=LiveCircuit(
                inventory_links=inventory_links,
                id=details.id,
                name=details.name,
                description=details.description,
                active=details.active,
                endpoints=endpoints,
                layout=layout,
                issues=reader.issues,
            ),
        )
    if isinstance(job, LiveRequest):
        if require() != approved:
            raise AuthorizationError("company policy changed during the live read")
    elif (
        repository.require_portal_owner_source(
            connection,
            source_profile_id=str(job.source_profile_id),
            grant_id=str(job.grant_id),
            layout=layout_enabled,
        )
        != profile
    ):
        raise AuthorizationError("owner source changed during the live read")
    return result
