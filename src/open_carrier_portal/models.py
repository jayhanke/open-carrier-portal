from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RelationshipState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class RecordState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class ConnectorIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str
    organization_name: str
    connector_id: str
    issuer_url: str
    enabled: bool = True


class Device(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    type: str | None = None
    vendor: str | None = None
    model: str | None = None
    operating_system: str | None = None
    operational_status: str | None = None
    site: str | None = None
    last_observed_at: datetime | None = None


class Interface(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    device_id: str
    name: str
    description: str | None = None
    speed_bps: int | None = Field(default=None, ge=0)
    administrative_status: str | None = None
    operational_status: str | None = None
    optical_rx_dbm: float | None = None
    optical_tx_dbm: float | None = None
    last_observed_at: datetime | None = None


class Alarm(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    resource_id: str | None = None
    severity: str
    state: str
    summary: str
    raised_at: datetime | None = None
    cleared_at: datetime | None = None


class MetricPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    value: float


class MetricSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    resource_id: str
    metric: str
    unit: str
    start: datetime
    end: datetime
    step_seconds: int = Field(ge=1)
    points: list[MetricPoint]


class CircuitCompany(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str


class CircuitLocation(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    floor: str | None = None
    suite: str | None = None


class CircuitSite(BaseModel):
    """Rack context, retaining the existing site response shape for compatibility."""

    model_config = ConfigDict(frozen=True)
    id: str
    name: str | None = None
    site_code: str | None = None
    rack_name: str | None = None
    company: CircuitCompany | None = None
    location: CircuitLocation | None = None


class CircuitRackUnit(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str | None = None
    site: CircuitSite | None = None


class CircuitDeviceStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    vendor: str | None = None
    model: str | None = None
    operating_system: str | None = None
    operational_status: str | None = None
    last_observed_at: datetime | None = None


class CircuitDevice(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    type: str | None = None
    company: CircuitCompany | None = None
    site: CircuitSite | None = None
    rack_units: list[CircuitRackUnit] = Field(default_factory=list)
    monitoring_state: Literal["available", "not_linked", "not_configured", "unavailable"] = (
        "not_linked"
    )
    monitoring: CircuitDeviceStatus | None = None


class CircuitPortStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str | None = None
    description: str | None = None
    speed_bps: int | None = Field(default=None, ge=0)
    administrative_status: str | None = None
    operational_status: str | None = None
    optical_rx_dbm: float | None = None
    optical_tx_dbm: float | None = None
    last_observed_at: datetime | None = None
    optical_state: Literal["available", "not_reported"] = "not_reported"


class CircuitPort(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str | None = None
    device: CircuitDevice | None = None
    owner_company: CircuitCompany | None = None
    connector_type: str | None = None
    monitoring_state: Literal["available", "not_linked", "not_configured", "unavailable"] = (
        "not_linked"
    )
    monitoring: CircuitPortStatus | None = None


class CircuitPathItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    position: int = Field(ge=0)
    kind: Literal["port", "patch", "pseudowire"]
    resource_id: str
    name: str | None = None
    device_id: str | None = None
    patch_type: str | int | None = None
    port_a_id: str | None = None
    port_b_id: str | None = None
    port: CircuitPort | None = None
    port_a: CircuitPort | None = None
    port_b: CircuitPort | None = None
    port_a_secondary_id: str | None = None
    port_b_secondary_id: str | None = None
    port_a_secondary: CircuitPort | None = None
    port_b_secondary: CircuitPort | None = None


class CircuitPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    circuit_id: str
    visibility: Literal["endpoints", "sfn_path", "full_shared_path"]
    items: list[CircuitPathItem]
    mapping_state: Literal["complete", "incomplete"] = "incomplete"
    omitted_enrichment: list[str] = Field(default_factory=list)


class CircuitDetails(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str | None = None
    description: str | None = None
    service_name: str | None = None
    service_type: str | None = None
    bandwidth: str | None = None
    active: bool | None = None
    a_location: CircuitSite | None = None
    z_location: CircuitSite | None = None
    path: CircuitPath | None = None
    omitted_enrichment: list[str] = Field(default_factory=list)


class CapabilitySet(BaseModel):
    model_config = ConfigDict(frozen=True)

    adapter: str
    adapter_version: str
    resources: list[str]
    metrics: list[str]


class Page[T](BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_cursor: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str
    retry_after_seconds: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
