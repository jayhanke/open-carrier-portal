from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from open_carrier_portal.repository import SourceDatabaseProfile

POLICY_LIMIT = 100
POLICY_LEASE_SECONDS = 90


class CompanyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: UUID
    revision: int = Field(ge=1, strict=True)
    owner_organization_id: UUID
    recipient_organization_id: UUID
    recipient_name: str = Field(min_length=1, max_length=120)
    connector_id: UUID
    state: Literal["pending", "active", "needs_approval", "revoked"]
    resource: Literal["circuits"] = "circuits"
    company_field: Literal["billing_company_id"] = "billing_company_id"
    visibility: Literal["endpoints", "full_shared_path"] = "endpoints"
    include_status: Literal[True] = True


class PolicyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: UUID
    revision: int = Field(ge=1, strict=True)
    state: Literal["accepted", "needs_approval"]
    source_profile_id: UUID | None = None
    source_company_id: str | None = Field(default=None, min_length=1, max_length=2000)
    mapping_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    visibility: Literal["endpoints", "full_shared_path"] = "endpoints"

    @model_validator(mode="after")
    def require_approved_identity(self) -> Self:
        if self.state == "accepted" and (
            self.source_profile_id is None
            or not self.source_company_id
            or not self.source_company_id.strip()
            or self.mapping_fingerprint is None
        ):
            raise ValueError("accepted policies require a source profile, company and fingerprint")
        if self.state == "needs_approval" and any(
            value is not None
            for value in (self.source_profile_id, self.source_company_id, self.mapping_fingerprint)
        ):
            raise ValueError("unapproved policies cannot carry an effective company link")
        return self


class PolicySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    policies: list[CompanyPolicy] = Field(max_length=POLICY_LIMIT)


class CircuitAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    company_id: UUID
    name: str = Field(min_length=1, max_length=120)


class PortalCompanyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: UUID
    name: str = Field(min_length=1, max_length=120)
    policy_id: UUID | None = None
    revision: int = Field(default=0, ge=0, strict=True)
    visibility: Literal["endpoints", "full_shared_path"] = "endpoints"

    @model_validator(mode="after")
    def require_policy_revision(self) -> Self:
        if (self.policy_id is None) != (self.revision == 0):
            raise ValueError("existing company links require a policy and revision")
        return self


class PortalCompanyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: UUID
    policies: list[CompanyPolicy] = Field(max_length=POLICY_LIMIT)


@dataclass(frozen=True)
class LocalCompanyPolicy:
    policy: CompanyPolicy
    receipt: PolicyReceipt | None
    synced_at: float
    approved_by: str | None
    was_approved: bool


def source_fingerprint(profile: SourceDatabaseProfile) -> str:
    identity = {
        "profile_id": profile.profile_id,
        "driver": profile.driver,
        "host": profile.host,
        "port": profile.port,
        "database_name": profile.database_name,
        "username": profile.username,
        "password_env_var": profile.password_env_var,
        "mapping": json.loads(profile.mapping_json),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
