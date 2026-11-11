from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_RESULT_BYTES = 1_048_576


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


InventoryResource = Literal["locations", "racks", "devices", "ports", "companies", "circuits"]
INVENTORY_ID = r"^(location|company_location|device|port|company|circuit)_[a-f0-9]{24}$"


class InventoryRef(WireModel):
    resource: InventoryResource
    id: str = Field(pattern=INVENTORY_ID)
    name: str = Field(min_length=1, max_length=512)


class InventoryAttribute(WireModel):
    label: str = Field(max_length=80)
    value: str = Field(max_length=4000)


class InventoryNode(InventoryRef):
    attributes: list[InventoryAttribute] = Field(default_factory=list, max_length=20)
    parents: list[InventoryRef] = Field(default_factory=list, max_length=6)


class InventoryRelation(WireModel):
    label: str = Field(max_length=80)
    resource: InventoryResource
    parent: InventoryRef


class InventoryView(WireModel):
    resource: InventoryResource
    selected: InventoryNode | None = None
    parent: InventoryRef | None = None
    listing_resource: InventoryResource
    items: list[InventoryNode] = Field(max_length=100)
    next_offset: int | None = Field(default=None, ge=0, le=1_000_000, strict=True)
    relations: list[InventoryRelation] = Field(default_factory=list, max_length=6)
    issues: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def consistent_items(self) -> Self:
        if any(item.resource != self.listing_resource for item in self.items):
            raise ValueError("inventory items must match their listing resource")
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("inventory item IDs must be unique")
        return self


class CircuitSummary(WireModel):
    id: str = Field(pattern=r"^circuit_[a-f0-9]{24}$")
    name: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=1000)
    billing_company_name: str | None = Field(default=None, max_length=128)


class MonitoringStatus(WireModel):
    state: Literal["available", "unavailable", "not_linked"]
    operational_status: str | None = Field(default=None, max_length=120)
    administrative_status: str | None = Field(default=None, max_length=120)
    optical_rx_dbm: float | None = None
    optical_tx_dbm: float | None = None
    last_observed_at: datetime | None = None
    speed_bps: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def unavailable_has_no_status(self) -> Self:
        if self.state != "available" and any(
            value is not None for key, value in self.model_dump().items() if key != "state"
        ):
            raise ValueError("unavailable monitoring cannot carry a healthy status")
        return self


class LivePort(WireModel):
    port_id: str = Field(pattern=r"^port_[a-f0-9]{24}$")
    port_name: str | None = Field(default=None, max_length=512)
    device_id: str = Field(pattern=r"^device_[a-f0-9]{24}$")
    device_name: str = Field(max_length=512)
    port_status: MonitoringStatus
    device_status: MonitoringStatus
    rack: InventoryRef | None = None
    location: InventoryRef | None = None


class CircuitEndpoint(LivePort):
    position: Literal["a", "z"]


class UnavailableEndpoint(WireModel):
    position: Literal["a", "z"]
    issue: Literal["missing_reference"] = "missing_reference"
    port_id: None = None
    port_name: None = None
    device_id: None = None
    device_name: None = None
    rack: None = None
    location: None = None
    port_status: MonitoringStatus = Field(
        default_factory=lambda: MonitoringStatus(state="unavailable")
    )
    device_status: MonitoringStatus = Field(
        default_factory=lambda: MonitoringStatus(state="unavailable")
    )

    @model_validator(mode="after")
    def no_invented_status(self) -> Self:
        if any(status.state != "unavailable" for status in (self.port_status, self.device_status)):
            raise ValueError("missing endpoints cannot carry monitoring readings or identity")
        return self


class LayoutPort(LivePort):
    role: Literal["port", "a", "z", "a_secondary", "z_secondary"]


PathItemIssue = Literal["unsupported_type", "mapping_missing", "missing_reference"]


class LivePathItem(WireModel):
    position: int = Field(ge=0, strict=True)
    kind: Literal["port", "patch", "pseudowire", "dark_fiber", "unresolved", "blank"]
    name: str | None = Field(default=None, max_length=512)
    issue: PathItemIssue | None = None
    patch_type: str | None = Field(default=None, max_length=120)
    ports: list[LayoutPort] = Field(max_length=4)

    @model_validator(mode="after")
    def valid_ports(self) -> Self:
        roles = [port.role for port in self.ports]
        if self.kind == "blank":
            if (
                self.name is not None
                or self.issue is not None
                or self.patch_type is not None
                or roles
            ):
                raise ValueError("blank path slots cannot carry labels, issues or readings")
            return self
        if self.kind == "unresolved":
            if self.issue is None or roles or self.patch_type is not None or not self.name:
                raise ValueError("unresolved path nodes require a label and issue, not readings")
            return self
        if self.issue is not None:
            raise ValueError("only unresolved path nodes may carry an issue")
        if self.kind == "dark_fiber":
            if roles or self.patch_type is not None:
                raise ValueError("dark-fiber segments carry a description, not port readings")
            return self
        if (self.kind == "port" and roles != ["port"]) or (
            self.kind != "port"
            and (
                roles[:2] != ["a", "z"]
                or len(set(roles)) != len(roles)
                or any(role not in {"a_secondary", "z_secondary"} for role in roles[2:])
            )
        ):
            raise ValueError("path nodes require a port or both connected ports")
        return self


class LiveLayout(WireModel):
    items: list[LivePathItem] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def ordered_items(self) -> Self:
        positions = [item.position for item in self.items]
        component_positions = [item.position for item in self.items if item.kind != "blank"]
        if positions != sorted(positions) or len(component_positions) != len(
            set(component_positions)
        ):
            raise ValueError(
                "layout positions must increase; only blank slots may share a position"
            )
        return self


class LiveCircuit(CircuitSummary):
    inventory_links: bool = False
    description: str | None = Field(default=None, max_length=4000)
    active: bool | None = None
    endpoints: list[CircuitEndpoint | UnavailableEndpoint] = Field(min_length=2, max_length=2)
    layout: LiveLayout | None = None
    issues: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def ordered_endpoints(self) -> Self:
        if [endpoint.position for endpoint in self.endpoints] != ["a", "z"]:
            raise ValueError("circuit endpoints must be ordered a, z")
        return self


class CircuitPage(WireModel):
    items: list[CircuitSummary] = Field(max_length=100)
    issues: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=20
    )
    next_offset: int | None = Field(default=None, ge=0, le=1_000_000, strict=True)

    @model_validator(mode="after")
    def unique_circuit_ids(self) -> Self:
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("circuit page IDs must be unique")
        return self


class LiveQuery(WireModel):
    request_id: UUID
    connector_id: UUID
    owner_organization_id: UUID
    expires_at: float
    operation: Literal["circuits.list", "circuits.read"]
    circuit_id: str | None = Field(default=None, pattern=r"^circuit_[a-f0-9]{24}$")
    query: str = Field(default="", max_length=120)
    order: Literal["source", "name_asc", "name_desc"] = "source"
    offset: int = Field(default=0, ge=0, le=999_900, strict=True)
    limit: int = Field(default=50, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def valid_operation(self) -> Self:
        if (self.operation == "circuits.read") != (self.circuit_id is not None):
            raise ValueError("only a circuit read requires a circuit ID")
        return self


class LiveRequest(LiveQuery):
    audience: Literal["partner"] = "partner"
    recipient_organization_id: UUID
    policy_id: UUID
    revision: int = Field(ge=1, strict=True)
    visibility: Literal["endpoints", "full_shared_path"] = "endpoints"

    @model_validator(mode="after")
    def partner_only(self) -> Self:
        if self.owner_organization_id == self.recipient_organization_id:
            raise ValueError("partner requests cannot select the owner view")
        return self


class OwnerLiveRequest(LiveQuery):
    audience: Literal["owner"]
    source_profile_id: UUID
    grant_id: UUID


class OwnerSourcesRequest(WireModel):
    audience: Literal["owner_sources"]
    operation: Literal["sources.list"] = "sources.list"
    request_id: UUID
    connector_id: UUID
    owner_organization_id: UUID
    expires_at: float


class OwnerExploreRequest(WireModel):
    audience: Literal["owner_explore"]
    operation: Literal["inventory.browse"] = "inventory.browse"
    request_id: UUID
    connector_id: UUID
    owner_organization_id: UUID
    expires_at: float
    source_profile_id: UUID
    grant_id: UUID
    resource: InventoryResource
    record_id: str | None = Field(default=None, pattern=INVENTORY_ID)
    parent_resource: InventoryResource | None = None
    parent_id: str | None = Field(default=None, pattern=INVENTORY_ID)
    query: str = Field(default="", max_length=120)
    offset: int = Field(default=0, ge=0, le=999_900, strict=True)
    limit: int = Field(default=50, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def valid_context(self) -> Self:
        if (self.parent_resource is None) != (self.parent_id is None):
            raise ValueError("parent resource and ID must be supplied together")
        if self.record_id is not None and self.parent_id is not None:
            raise ValueError("select a record or a parent context, not both")
        return self


LiveJob = Annotated[
    LiveRequest | OwnerLiveRequest | OwnerSourcesRequest | OwnerExploreRequest,
    Field(discriminator="audience"),
]


class OwnerSource(WireModel):
    source_profile_id: UUID
    grant_id: UUID
    name: str = Field(min_length=1, max_length=512)
    explorer_enabled: bool = False
    layout_enabled: bool = False


class OwnerSourcePage(WireModel):
    items: list[OwnerSource] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({item.source_profile_id for item in self.items}) != len(self.items):
            raise ValueError("owner source IDs must be unique")
        return self


class LiveResult(WireModel):
    request_id: UUID
    circuit: LiveCircuit | None = None
    page: CircuitPage | None = None
    sources: OwnerSourcePage | None = None
    inventory: InventoryView | None = None
    error: Literal["access_denied", "not_found", "unavailable", "invalid_request"] | None = None

    @model_validator(mode="after")
    def exactly_one_result(self) -> Self:
        if (
            sum(
                value is not None
                for value in (self.circuit, self.page, self.sources, self.inventory, self.error)
            )
            != 1
        ):
            raise ValueError("exactly one result or error is required")
        return self
