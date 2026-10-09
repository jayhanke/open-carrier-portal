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


class CircuitPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    circuit_id: str
    visibility: Literal["endpoints", "sfn_path", "full_shared_path"]
    items: list[CircuitPathItem]


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
