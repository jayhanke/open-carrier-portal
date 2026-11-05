from __future__ import annotations

from pathlib import Path

from open_carrier_portal.circuits import CircuitReader
from open_carrier_portal.company_policies import source_fingerprint
from open_carrier_portal.portal_agent import PortalConnection
from open_carrier_portal.repository import Repository
from open_carrier_portal.source_database import configured_profile_database


def approve_owner_source(
    repository: Repository,
    portal: PortalConnection,
    *,
    metadata_path: Path,
    profile_name: str,
    approved_by: str,
    explorer_enabled: bool = False,
    layout_enabled: bool = False,
) -> None:
    profile = repository.get_source_database_profile(profile_name)
    if profile is None or not profile.enabled:
        raise ValueError("enabled database profile was not found")
    source = configured_profile_database(profile, metadata_path)
    CircuitReader(
        repository, source, None, allow_unresolved_path_items=layout_enabled
    ).require_view(
        "full_shared_path" if layout_enabled else "endpoints"
    )
    source.fetch_inventory_page("circuits", offset=0, limit=1)
    repository.approve_portal_owner_source(
        portal,
        profile_name=profile_name,
        fingerprint=source_fingerprint(profile),
        approved_by=approved_by,
        explorer_enabled=explorer_enabled,
        layout_enabled=layout_enabled,
    )
