from __future__ import annotations

from pathlib import Path

from open_carrier_portal.circuits import CircuitReader
from open_carrier_portal.company_policies import source_fingerprint
from open_carrier_portal.errors import AuthorizationError
from open_carrier_portal.portal_agent import PortalConnection
from open_carrier_portal.repository import Repository
from open_carrier_portal.source_database import configured_profile_database


def approve_company_policy(
    repository: Repository,
    portal: PortalConnection,
    *,
    metadata_path: Path,
    policy_id: str,
    revision: int,
    profile_name: str,
    company_id: str,
    approved_by: str,
    layout_consent: bool = False,
) -> None:
    profile = repository.get_source_database_profile(profile_name)
    if profile is None or not profile.enabled:
        raise ValueError("enabled database profile was not found")
    source = configured_profile_database(profile, metadata_path)
    local = next(
        (
            item
            for item in repository.list_portal_policies(portal)
            if str(item.policy.policy_id) == policy_id and item.policy.revision == revision
        ),
        None,
    )
    if local is None:
        raise ValueError("refresh the connector and select a current policy revision")
    if local.policy.visibility == "full_shared_path":
        if not layout_consent:
            raise ValueError("explicit full layout approval is required")
        CircuitReader(
            repository, source, None, allow_unresolved_path_items=True
        ).require_view("full_shared_path")
    if source.missing_fields_for(
        requirements={
            "companies": ("source_id", "name"),
            "circuits": ("source_id", "billing_company_id"),
        }
    ):
        raise ValueError("map Companies ID/name and Circuits ID/billing company before approval")
    companies = source.fetch_matching("companies", filters={"source_id": company_id}, limit=1)
    if len(companies) != 1 or not str(companies[0].get("name") or "").strip():
        raise ValueError("selected company must resolve to one named company in this database")
    repository.approve_portal_policy(
        portal,
        policy_id=policy_id,
        revision=revision,
        profile_name=profile_name,
        company_id=str(companies[0]["source_id"]),
        fingerprint=source_fingerprint(profile),
        approved_by=approved_by,
        layout_consent=layout_consent,
    )


def require_company_circuit(
    repository: Repository,
    portal: PortalConnection,
    *,
    metadata_path: Path,
    policy_id: str,
    revision: int,
    recipient_organization_id: str,
    circuit_source_id: str,
) -> None:
    """Validate a partner circuit against live ownership, never an owner-view switch."""
    receipt = repository.require_portal_company_policy(
        portal,
        policy_id=policy_id,
        revision=revision,
        recipient_organization_id=recipient_organization_id,
    )
    profile = repository.get_source_database_profile_by_id(str(receipt.source_profile_id))
    if profile is None or source_fingerprint(profile) != receipt.mapping_fingerprint:
        raise AuthorizationError("approved source database mapping is no longer current")
    source = configured_profile_database(profile, metadata_path)
    companies = source.fetch_matching(
        "companies", filters={"source_id": receipt.source_company_id}, limit=1
    )
    circuits = source.fetch_matching(
        "circuits",
        filters={
            "source_id": circuit_source_id,
            "billing_company_id": receipt.source_company_id,
        },
        limit=1,
    )
    if len(companies) != 1 or len(circuits) != 1:
        raise AuthorizationError("circuit is not currently billed to the approved company")
    current = repository.require_portal_company_policy(
        portal,
        policy_id=policy_id,
        revision=revision,
        recipient_organization_id=recipient_organization_id,
    )
    if current != receipt:
        raise AuthorizationError("company policy changed during authorization")
