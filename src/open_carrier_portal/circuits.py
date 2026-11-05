from __future__ import annotations

import logging
from typing import Any, Literal

from starlette.concurrency import run_in_threadpool

from open_carrier_portal.adapters.base import MonitoringAdapter
from open_carrier_portal.diagnostics import log_event, log_failure, record_data_issue
from open_carrier_portal.errors import (
    ConfigurationError,
    ConnectorError,
    IntegrationUnavailableError,
    MissingSourceReferenceError,
    RateLimitError,
    ResourceNotFoundError,
    SourceDatabaseError,
    SourceDatabaseQueryError,
    SourceDatabaseUnavailableError,
    UpstreamResponseError,
    UpstreamTimeoutError,
)
from open_carrier_portal.models import (
    CircuitCompany,
    CircuitDetails,
    CircuitDevice,
    CircuitDeviceStatus,
    CircuitLocation,
    CircuitPath,
    CircuitPathItem,
    CircuitPort,
    CircuitPortStatus,
    CircuitRackUnit,
    CircuitSite,
    PathItemIssue,
)
from open_carrier_portal.repository import Repository
from open_carrier_portal.source_contract import CORE_RESOURCES, SOURCE_RESOURCES
from open_carrier_portal.source_database import SourceDatabase

PathVisibility = Literal["endpoints", "sfn_path", "full_shared_path"]


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _observium_id(value: object) -> str | None:
    if value is None or value == 0:
        return None
    identifier = str(value).strip()
    return identifier if identifier and identifier != "0" else None


def _coordinate(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError as exc:
        raise UpstreamResponseError("A source location contains an invalid coordinate") from exc


def _active(value: object) -> bool | None:
    if value is None:
        return None
    if str(value).casefold() in {"1", "true", "yes"}:
        return True
    if str(value).casefold() in {"0", "false", "no"}:
        return False
    raise UpstreamResponseError("A source circuit contains an invalid active status")


class CircuitReader:
    """Read a single already-authorized circuit; caches last only for this request."""

    def __init__(
        self,
        repository: Repository,
        source: SourceDatabase,
        monitoring: MonitoringAdapter | None,
        *,
        integration: str = "nettool",
        monitoring_required: bool = True,
        allow_unresolved_path_items: bool = False,
    ) -> None:
        self.repository = repository
        self.source = source
        self.monitoring = monitoring
        self.integration = integration
        self.monitoring_required = monitoring_required
        self.allow_unresolved_path_items = allow_unresolved_path_items
        self._monitoring_unavailable = False
        self._ports: dict[str, CircuitPort] = {}
        self._devices: dict[str, CircuitDevice] = {}
        self._sites: dict[str, CircuitSite] = {}
        self._companies: dict[str, CircuitCompany] = {}
        self._endpoint_sites: tuple[CircuitSite | None, CircuitSite | None] | None = None
        self._rows_read = 0
        self._omitted: set[str] = set()
        self.issues: list[str] = []

    def _issue(self, message: str) -> None:
        record_data_issue(self.issues, message, event="circuit.data_unavailable")

    def _display_text(self, value: object, label: str, limit: int = 512) -> str | None:
        text = _text(value)
        if self.allow_unresolved_path_items and text is not None and len(text) > limit:
            self._issue(f"{label} was shortened to fit the circuit view.")
            return text[:limit]
        return text

    def _monitoring_failed(self, exc: ConnectorError) -> None:
        if self.monitoring_required:
            raise exc
        log_failure("observium", "circuit.status_unavailable", exc)
        if isinstance(exc, (IntegrationUnavailableError, UpstreamTimeoutError, RateLimitError)):
            self._monitoring_unavailable = True

    @property
    def missing_fields(self) -> dict[str, list[str]]:
        missing = self.source.missing_fields_for(monitoring=self.monitoring is not None)
        if self.allow_unresolved_path_items and "devices" in missing:
            fields = [field for field in missing["devices"] if field != "rack_id"]
            if fields:
                missing["devices"] = fields
            else:
                del missing["devices"]
        return missing

    def require_view(self, visibility: str) -> None:
        requirements = {"circuits": ("source_id", "name")}
        if visibility in {"endpoints", "sfn_path", "full_shared_path"}:
            requirements = {
                resource: SOURCE_RESOURCES[resource].required for resource in CORE_RESOURCES
            }
            if visibility != "endpoints" and not self.allow_unresolved_path_items:
                requirements["circuit_layout_items"] = SOURCE_RESOURCES[
                    "circuit_layout_items"
                ].required
            if self.allow_unresolved_path_items:
                for resource in ("racks", "locations"):
                    requirements.pop(resource)
                requirements["devices"] = ("source_id", "name")
        missing = self.source.missing_fields_for(
            monitoring=self.monitoring is not None, requirements=requirements
        )
        if missing:
            names = [
                resource + "." + field for resource, fields in missing.items() for field in fields
            ]
            raise UpstreamResponseError(
                "Map the fields needed for this circuit view: " + ", ".join(names)
            )

    def public_id(self, kind: str, source_id: object) -> str:
        return self.repository.ocp_resource_id(self.integration, kind, str(source_id))

    async def rows(
        self,
        resource: str,
        filters: dict[str, object],
        *,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        # Circuit columns are validated against the share's visibility before lookup.
        if resource != "circuits" and resource in self.missing_fields:
            raise UpstreamResponseError(f"The configured mapping for {resource} is incomplete")
        try:
            rows = await run_in_threadpool(
                self.source.fetch_matching,
                resource,
                filters,
                order_by=order_by,
                limit=1000,
            )
        except SourceDatabaseUnavailableError as exc:
            raise IntegrationUnavailableError("The source database is unavailable") from exc
        except (ConfigurationError, SourceDatabaseError) as exc:
            raise UpstreamResponseError("The source database mapping or query failed") from exc
        self._rows_read += len(rows)
        if self._rows_read > 5000:
            raise UpstreamResponseError("The shared circuit exceeds its source-record limit")
        return rows

    async def one(self, resource: str, source_id: object) -> dict[str, Any] | None:
        rows = await self.rows(resource, {"source_id": str(source_id)})
        if len(rows) > 1:
            raise UpstreamResponseError("A source identifier resolves to multiple records")
        return rows[0] if rows else None

    async def reference(
        self,
        resource: str,
        source_id: object,
        *,
        optional: bool = False,
    ) -> dict[str, Any] | None:
        if source_id is None:
            if optional:
                return None
            raise MissingSourceReferenceError(resource)
        if not self.source.has_resource(resource):
            if optional:
                self._omitted.add(resource)
                if self.allow_unresolved_path_items:
                    self._issue(f"{resource.replace('_', ' ').title()} details are unavailable.")
                return None
            raise UpstreamResponseError(f"The circuit needs a source mapping for {resource}")
        if resource in self.missing_fields:
            if optional and self.allow_unresolved_path_items:
                self._omitted.add(resource)
                self._issue(f"{resource.replace('_', ' ').title()} mapping is incomplete.")
                return None
            raise UpstreamResponseError(f"The configured mapping for {resource} is incomplete")
        try:
            row = await self.one(resource, source_id)
        except UpstreamResponseError as exc:
            if not (
                optional
                and self.allow_unresolved_path_items
                and isinstance(exc.__cause__, (ConfigurationError, SourceDatabaseQueryError))
            ):
                raise
            self._omitted.add(resource)
            self._issue(f"{resource.replace('_', ' ').title()} enrichment could not be read.")
            return None
        if row is None:
            if optional and self.allow_unresolved_path_items:
                self._omitted.add(resource)
                self._issue(f"A referenced {resource.replace('_', ' ')} record is missing.")
                return None
            raise MissingSourceReferenceError(resource)
        return row

    async def company(self, source_id: object) -> CircuitCompany | None:
        if source_id is None:
            return None
        key = str(source_id)
        if key not in self._companies:
            row = await self.reference("companies", source_id, optional=True)
            if row is None:
                return None
            if row.get("name") is None:
                if self.allow_unresolved_path_items:
                    self._issue("A referenced company name is unavailable.")
                    return None
                raise UpstreamResponseError("A source company has no name")
            self._companies[key] = CircuitCompany(
                id=self.public_id("company", key),
                name=str(row["name"]),
            )
        return self._companies[key]

    async def site(self, source_id: object, *, required: bool = False) -> CircuitSite | None:
        if source_id is None:
            if required:
                if self.allow_unresolved_path_items:
                    self._issue("A device's rack reference is missing.")
                    return None
                raise UpstreamResponseError("A shared resource has no rack reference")
            return None
        key = str(source_id)
        if key not in self._sites:
            row = await self.reference(
                "racks", source_id, optional=not required or self.allow_unresolved_path_items
            )
            if row is None:
                return None
            location = await self.reference(
                "locations", row.get("location_id"), optional=self.allow_unresolved_path_items
            )
            if row.get("location_id") is None and self.allow_unresolved_path_items:
                self._issue("A rack's location reference is missing.")

            def coordinate(field: str, limit: int) -> float | None:
                value = location.get(field) if location else None
                try:
                    parsed = _coordinate(value)
                    if parsed is not None and not -limit <= parsed <= limit:
                        raise UpstreamResponseError("A source coordinate is out of range")
                    return parsed
                except UpstreamResponseError:
                    if not self.allow_unresolved_path_items:
                        raise
                    self._issue("Location coordinates are invalid and were not displayed.")
                    return None

            physical = (
                None
                if location is None
                else CircuitLocation(
                    id=self.public_id("location", location["source_id"]),
                    address1=_text(location.get("address1")),
                    address2=_text(location.get("address2")),
                    city=_text(location.get("city")),
                    state=_text(location.get("state")),
                    postal_code=_text(location.get("postal_code")),
                    floor=_text(location.get("floor")),
                    suite=_text(location.get("suite")),
                    latitude=coordinate("latitude", 90),
                    longitude=coordinate("longitude", 180),
                )
            )
            self._sites[key] = CircuitSite(
                id=self.public_id("company_location", key),
                name=_text(row.get("name")),
                site_code=_text(row.get("site_code")),
                rack_name=_text(row.get("rack_name", row.get("name"))),
                company=await self.company(row.get("company_id")),
                location=physical,
            )
        return self._sites[key]

    async def device(self, source_id: object) -> CircuitDevice:
        if source_id is None:
            raise MissingSourceReferenceError("devices")
        key = str(source_id)
        if key not in self._devices:
            row = await self.reference("devices", source_id)
            assert row is not None
            if row.get("name") is None:
                if not self.allow_unresolved_path_items:
                    raise UpstreamResponseError("A source device has no name")
                self._issue(
                    "A device name is unavailable; its verified source identity was retained."
                )
            if self.source.has_resource("rack_positions") and (
                not self.allow_unresolved_path_items or "rack_positions" not in self.missing_fields
            ):
                racks = await self.rows("rack_positions", {"device_id": key}, order_by="source_id")
            else:
                self._omitted.add("rack_units")
                if self.allow_unresolved_path_items:
                    self._issue("Rack-unit details are unavailable.")
                racks = []
            device = CircuitDevice(
                id=self.public_id("device", key),
                name=self._display_text(row.get("name"), "A device name") or "Name unavailable",
                type=_text(row.get("type")),
                company=await self.company(row.get("company_id")),
                site=await self.site(row.get("rack_id"), required=True),
                rack_units=[
                    CircuitRackUnit(
                        id=self.public_id("rack_unit", rack["source_id"]),
                        name=_text(rack.get("name")),
                        site=await self.site(rack.get("rack_id")),
                    )
                    for rack in racks
                ],
            )
            observed_id = _observium_id(row.get("observium_id"))
            if observed_id is not None:
                if self.monitoring is None or self._monitoring_unavailable:
                    device = device.model_copy(
                        update={
                            "monitoring_state": "unavailable"
                            if self._monitoring_unavailable
                            else "not_configured",
                        }
                    )
                else:
                    public_id = self.repository.ocp_resource_id(
                        "observium",
                        "device",
                        observed_id,
                    )
                    try:
                        observed = await self.monitoring.get_device(public_id)
                    except (
                        IntegrationUnavailableError,
                        UpstreamTimeoutError,
                        RateLimitError,
                        ResourceNotFoundError,
                        UpstreamResponseError,
                    ) as exc:
                        self._monitoring_failed(exc)
                        self._devices[key] = device.model_copy(
                            update={"monitoring_state": "unavailable"}
                        )
                        return self._devices[key]
                    if observed.id != public_id:
                        raise UpstreamResponseError("Monitoring returned a different device")
                    device = device.model_copy(
                        update={
                            "monitoring_state": "available",
                            "monitoring": CircuitDeviceStatus(
                                vendor=observed.vendor,
                                model=observed.model,
                                operating_system=observed.operating_system,
                                operational_status=observed.operational_status,
                                last_observed_at=observed.last_observed_at,
                            ),
                        }
                    )
            self._devices[key] = device
        return self._devices[key]

    async def port(self, source_id: object) -> CircuitPort:
        if source_id is None:
            raise MissingSourceReferenceError("ports")
        key = str(source_id)
        if key not in self._ports:
            row = await self.reference("ports", key)
            assert row is not None
            if row.get("name") is None:
                if not self.allow_unresolved_path_items:
                    raise UpstreamResponseError("A source port has no name")
                self._issue(
                    "A port name is unavailable; its verified source identity was retained."
                )
            port = CircuitPort(
                id=self.public_id("port", key),
                name=self._display_text(row.get("name"), "A port name"),
                device=await self.device(row.get("device_id")),
                owner_company=await self.company(row.get("owner_company_id")),
                connector_type=_text(row.get("connector_type")),
            )
            observed_id = _observium_id(row.get("observium_id"))
            if observed_id is not None:
                if self.monitoring is None or self._monitoring_unavailable:
                    port = port.model_copy(
                        update={
                            "monitoring_state": "unavailable"
                            if self._monitoring_unavailable
                            else "not_configured",
                        }
                    )
                else:
                    device_row = await self.reference("devices", row["device_id"])
                    assert device_row is not None
                    device_observed_id = _observium_id(device_row.get("observium_id"))
                    if device_observed_id is None:
                        if self.monitoring_required:
                            raise UpstreamResponseError(
                                "A monitored port requires its device's Observium ID"
                            )
                        log_event(
                            "observium",
                            "circuit.device_link_missing",
                            "Port status cannot be verified without its device's Observium ID. "
                            "The layout is retained with unavailable port readings.",
                            level=logging.WARNING,
                        )
                        self._ports[key] = port.model_copy(
                            update={"monitoring_state": "unavailable"}
                        )
                        return self._ports[key]
                    public_id = self.repository.ocp_resource_id(
                        "observium",
                        "interface",
                        observed_id,
                    )
                    try:
                        observed = await self.monitoring.get_interface(public_id)
                    except (
                        IntegrationUnavailableError,
                        UpstreamTimeoutError,
                        RateLimitError,
                        ResourceNotFoundError,
                        UpstreamResponseError,
                    ) as exc:
                        self._monitoring_failed(exc)
                        self._ports[key] = port.model_copy(
                            update={"monitoring_state": "unavailable"}
                        )
                        return self._ports[key]
                    if observed.id != public_id:
                        raise UpstreamResponseError("Monitoring returned a different interface")
                    if observed.device_id != self.repository.ocp_resource_id(
                        "observium", "device", device_observed_id
                    ):
                        raise UpstreamResponseError("Monitoring interface/device linkage differs")
                    port = port.model_copy(
                        update={
                            "monitoring_state": "available",
                            "monitoring": CircuitPortStatus(
                                name=observed.name,
                                description=observed.description,
                                speed_bps=observed.speed_bps,
                                administrative_status=observed.administrative_status,
                                operational_status=observed.operational_status,
                                optical_rx_dbm=observed.optical_rx_dbm,
                                optical_tx_dbm=observed.optical_tx_dbm,
                                last_observed_at=observed.last_observed_at,
                                optical_state=(
                                    "available"
                                    if observed.optical_rx_dbm is not None
                                    or observed.optical_tx_dbm is not None
                                    else "not_reported"
                                ),
                            ),
                        }
                    )
            self._ports[key] = port
        return self._ports[key]

    async def path(
        self,
        circuit_id: str,
        circuit: dict[str, Any],
        visibility: PathVisibility,
    ) -> CircuitPath:
        self.require_view(visibility)
        try:
            layout = await self.reference("circuit_layouts", circuit.get("circuit_layout_id"))
        except MissingSourceReferenceError:
            if not self.allow_unresolved_path_items:
                raise
            self._issue("The circuit layout reference is missing or its record was not found.")
            layout = {
                "source_id": circuit.get("circuit_layout_id"),
                "port_a_id": None,
                "port_z_id": None,
            }
        assert layout is not None
        endpoints: list[CircuitPort | None] = []
        for side, field in (("A", "port_a_id"), ("Z", "port_z_id")):
            try:
                endpoint = await self.port(layout.get(field))
            except MissingSourceReferenceError:
                if not self.allow_unresolved_path_items:
                    raise
                self._issue(
                    f"Endpoint {side} details are unavailable: a source reference is missing."
                )
                endpoint = None
            endpoints.append(endpoint)
        self._endpoint_sites = (
            endpoints[0].device.site if endpoints[0] and endpoints[0].device else None,
            endpoints[1].device.site if endpoints[1] and endpoints[1].device else None,
        )
        complete = not self.missing_fields
        items: list[CircuitPathItem] = []

        def add_unresolved(position: int, label: str, issue: PathItemIssue) -> None:
            nonlocal complete
            complete = False
            items.append(
                CircuitPathItem(
                    position=position,
                    kind="unresolved",
                    resource_id=self.public_id("layout_item", f"{circuit_id}:{position}"),
                    name=label[:512] or "Unknown component",
                    issue=issue,
                )
            )
            log_event(
                "portal",
                "path.item_unresolved",
                "Layout item details are unavailable; its label and position were retained.",
                level=logging.WARNING,
                position=position,
                reason=issue,
            )

        async def add_port(source_id: object, position: int) -> None:
            port = await self.port(source_id)
            row = await self.reference("ports", source_id)
            assert row is not None
            items.append(
                CircuitPathItem(
                    position=position,
                    kind="port",
                    resource_id=port.id,
                    name=port.name,
                    device_id=(
                        self.public_id("device", row["device_id"])
                        if row.get("device_id") is not None
                        else None
                    ),
                    port=port,
                )
            )

        async def secondary_port(
            identifier: object, side: str, position: int
        ) -> CircuitPort | None:
            nonlocal complete
            if identifier in (None, 0, "0"):
                return None
            try:
                return await self.port(identifier)
            except MissingSourceReferenceError:
                if not self.allow_unresolved_path_items:
                    raise
                complete = False
                self._issue(
                    f"Layout position {position}: secondary {side} port details "
                    "are unavailable because a source reference is missing."
                )
                return None

        if visibility == "endpoints":
            for position, endpoint in enumerate(endpoints):
                if endpoint is None:
                    add_unresolved(
                        position,
                        "Endpoint A" if position == 0 else "Endpoint Z",
                        "missing_reference",
                    )
                else:
                    await add_port(
                        layout.get("port_a_id" if position == 0 else "port_z_id"), position
                    )
        else:
            unavailable_issue: PathItemIssue = "missing_reference"
            if self.allow_unresolved_path_items and (
                not self.source.has_resource("circuit_layout_items")
                or "circuit_layout_items" in self.missing_fields
            ):
                self._issue(
                    "Ordered layout steps are unavailable because their mapping is incomplete."
                )
                unavailable_issue = "mapping_missing"
                rows = []
            else:
                rows = (
                    await self.rows(
                        "circuit_layout_items",
                        {"circuit_layout_id": str(layout["source_id"])},
                        order_by="position",
                    )
                    if layout["source_id"] is not None
                    else []
                )
            if not rows:
                if not self.allow_unresolved_path_items:
                    raise UpstreamResponseError("The shared circuit has no ordered path steps")
                self._issue("The circuit has no readable ordered layout steps.")
                add_unresolved(0, "Circuit layout", unavailable_issue)
            components: dict[
                str, tuple[str, Literal["patch", "pseudowire", "dark_fiber"]]
            ] = {
                "patch": ("patches", "patch"),
                "pw": ("pseudowires", "pseudowire"),
                "pseudowire": ("pseudowires", "pseudowire"),
                "newdarkfibercircuit": ("new_dark_fiber_circuits", "dark_fiber"),
            }
            for row in rows:
                source_id = row.get("item_source_id")
                try:
                    position = int(row["position"])
                except (KeyError, ValueError, TypeError) as exc:
                    raise UpstreamResponseError("A path item has an invalid position") from exc
                label = str(row.get("item_type") or "").strip()
                kind = label.casefold()
                if source_id is None:
                    if self.allow_unresolved_path_items:
                        add_unresolved(position, label, "missing_reference")
                        continue
                    raise UpstreamResponseError("A path item has no source reference")
                if kind == "port":
                    try:
                        await add_port(source_id, position)
                    except MissingSourceReferenceError:
                        if not self.allow_unresolved_path_items:
                            raise
                        add_unresolved(position, label, "missing_reference")
                    continue
                configuration = components.get(kind)
                if configuration is None:
                    if self.allow_unresolved_path_items:
                        add_unresolved(position, label, "unsupported_type")
                        continue
                    raise UpstreamResponseError(
                        "The circuit path contains an unsupported item type"
                    )
                resource, component_kind = configuration
                if self.allow_unresolved_path_items:
                    if not self.source.has_resource(resource) or resource in self.missing_fields:
                        add_unresolved(position, label, "mapping_missing")
                        continue
                    component = await self.one(resource, source_id)
                    if component is None:
                        add_unresolved(position, label, "missing_reference")
                        continue
                else:
                    component = await self.reference(resource, source_id)
                assert component is not None
                if component_kind == "dark_fiber":
                    items.append(
                        CircuitPathItem(
                            position=position,
                            kind="dark_fiber",
                            resource_id=self.public_id("dark_fiber", source_id),
                            name=self._display_text(
                                component.get("name"), "A dark-fiber description"
                            ),
                        )
                    )
                    continue
                a_id, z_id = component.get("port_a_id"), component.get("port_b_id")
                try:
                    a = await self.port(a_id)
                    z = await self.port(z_id)
                except MissingSourceReferenceError:
                    if not self.allow_unresolved_path_items:
                        raise
                    add_unresolved(position, label, "missing_reference")
                    continue
                a2_id = component.get("port_a_secondary_id")
                z2_id = component.get("port_b_secondary_id")
                a2 = await secondary_port(a2_id, "A", position)
                z2 = await secondary_port(z2_id, "Z", position)
                items.append(
                    CircuitPathItem(
                        position=position,
                        kind=component_kind,
                        resource_id=self.public_id(component_kind, source_id),
                        name=self._display_text(
                            component.get("pseudowire_id"), "A pseudowire label"
                        ),
                        patch_type=self._display_text(
                            component.get("patch_type"), "A patch type", 120
                        ),
                        port_a_id=a.id if a else None,
                        port_b_id=z.id if z else None,
                        port_a=a,
                        port_b=z,
                        port_a_secondary_id=a2.id if a2 else None,
                        port_b_secondary_id=z2.id if z2 else None,
                        port_a_secondary=a2,
                        port_b_secondary=z2,
                    )
                )
        return CircuitPath(
            circuit_id=circuit_id,
            visibility=visibility,
            items=items,
            mapping_state="complete" if complete else "incomplete",
            omitted_enrichment=sorted(self._omitted),
        )

    async def details(
        self,
        circuit_id: str,
        circuit: dict[str, Any],
        visibility: str,
    ) -> CircuitDetails:
        self.require_view(visibility)
        service = await self.reference("services", circuit.get("service_id"), optional=True)
        path = None
        if visibility in {"endpoints", "sfn_path", "full_shared_path"}:
            path = await self.path(
                circuit_id,
                circuit,
                "endpoints"
                if visibility == "endpoints"
                else ("sfn_path" if visibility == "sfn_path" else "full_shared_path"),
            )
        if path is not None:
            assert self._endpoint_sites is not None
            a_location, z_location = self._endpoint_sites
        else:
            a_location = await self.site(circuit.get("a_company_location_id"))
            z_location = await self.site(circuit.get("z_company_location_id"))
        try:
            active = _active(circuit.get("active"))
        except UpstreamResponseError:
            if not self.allow_unresolved_path_items:
                raise
            self._issue("Inventory active status is invalid and is shown as unknown.")
            active = None
        return CircuitDetails(
            id=circuit_id,
            name=self._display_text(circuit.get("name"), "The circuit name"),
            description=self._display_text(
                circuit.get("description"), "The circuit description", 4000
            ),
            active=active,
            service_name=_text(service.get("name")) if service else None,
            service_type=_text(service.get("type")) if service else None,
            bandwidth=_text(service.get("bandwidth")) if service else None,
            a_location=a_location,
            z_location=z_location,
            path=path,
            omitted_enrichment=sorted(self._omitted),
            issues=list(self.issues),
        )
