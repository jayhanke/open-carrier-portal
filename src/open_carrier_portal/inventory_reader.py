from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from open_carrier_portal.circuits import _active
from open_carrier_portal.company_policies import source_fingerprint
from open_carrier_portal.diagnostics import record_data_issue
from open_carrier_portal.errors import (
    ConfigurationError,
    ResourceNotFoundError,
    SourceDatabaseQueryError,
    UpstreamResponseError,
)
from open_carrier_portal.live_contract import (
    InventoryAttribute,
    InventoryNode,
    InventoryRef,
    InventoryRelation,
    InventoryResource,
    InventoryView,
    LiveResult,
    OwnerExploreRequest,
)
from open_carrier_portal.portal_agent import PortalConnection
from open_carrier_portal.repository import Repository
from open_carrier_portal.source_database import configured_profile_database

KINDS = {
    "locations": "location",
    "racks": "company_location",
    "devices": "device",
    "ports": "port",
    "companies": "company",
    "circuits": "circuit",
}
PARENTS: dict[InventoryResource, tuple[InventoryResource, str]] = {
    "racks": ("locations", "location_id"),
    "devices": ("racks", "rack_id"),
    "ports": ("devices", "device_id"),
}
CHILDREN: dict[InventoryResource, InventoryResource] = {
    "locations": "racks",
    "racks": "devices",
    "devices": "ports",
    "ports": "circuits",
    "companies": "racks",
}
FILTERS = {
    ("locations", "racks"): "location_id",
    ("racks", "devices"): "rack_id",
    ("devices", "ports"): "device_id",
    ("companies", "racks"): "company_id",
    ("companies", "devices"): "company_id",
    ("companies", "ports"): "owner_company_id",
    ("companies", "circuits"): "billing_company_id",
}
ATTRIBUTES = {
    "locations": ("address1", "address2", "city", "state", "postal_code", "floor", "suite"),
    "racks": ("site_code",),
    "devices": ("type",),
    "ports": ("connector_type", "vlan", "nni"),
    "companies": (),
    "circuits": (),
}


def read_inventory(
    repository: Repository,
    connection: PortalConnection,
    job: OwnerExploreRequest,
    metadata_path: Path,
) -> LiveResult:
    def require():
        return repository.require_portal_owner_source(
            connection,
            source_profile_id=str(job.source_profile_id),
            grant_id=str(job.grant_id),
            explorer=True,
        )

    profile = require()
    namespace = f"portal-owner:{profile.profile_id}:{source_fingerprint(profile)}:{job.grant_id}"
    source = configured_profile_database(
        replace(profile, timeout_seconds=min(profile.timeout_seconds, 10)),
        metadata_path,
    )
    issues: list[str] = []

    def issue(message: str) -> None:
        record_data_issue(issues, message, event="inventory.data_unavailable")

    def row_for(resource: InventoryResource, identifier: str) -> dict[str, Any]:
        rows = source.fetch_matching(resource, {"source_id": identifier}, limit=1)
        if len(rows) > 1:
            raise UpstreamResponseError("inventory identifier resolves to multiple rows")
        if not rows:
            raise ResourceNotFoundError()
        return rows[0]

    def decode(resource: InventoryResource, identifier: str) -> str:
        result = repository.source_resource_id(identifier, namespace, KINDS[resource])
        if result is None:
            raise ResourceNotFoundError()
        return result

    def ref(resource: InventoryResource, row: dict[str, Any]) -> InventoryRef:
        key = row.get("source_id")
        if key is None or not str(key).strip():
            raise UpstreamResponseError("inventory identifiers must be nonempty")
        if resource == "locations":
            name = ", ".join(
                str(row[field]) for field in ("city", "state", "address1") if row.get(field)
            )
        else:
            name = str(row.get("name") or "Name unavailable")
            if not row.get("name"):
                issue(f"A {resource.rstrip('s')} name is unavailable.")
        if len(name) > 512:
            issue("Some display names were shortened to fit the inventory view.")
            name = name[:512]
        return InventoryRef(
            resource=resource,
            id=repository.ocp_resource_id(namespace, KINDS[resource], str(key)),
            name=name or "Site",
        )

    def attributes(resource: InventoryResource, row: dict[str, Any]) -> list[InventoryAttribute]:
        fields = [
            InventoryAttribute(label=field.replace("_", " ").title(), value=str(row[field])[:4000])
            for field in ATTRIBUTES[resource]
            if row.get(field) is not None
        ]
        if any(
            len(str(row[field])) > 4000
            for field in ATTRIBUTES[resource] if row.get(field) is not None
        ):
            issue("Some inventory attributes were shortened to fit the view.")
        if resource == "circuits":
            try:
                active = _active(row.get("active"))
            except UpstreamResponseError:
                issue("Some circuit inventory statuses are invalid and are shown as unknown.")
                active = None
            fields.append(
                InventoryAttribute(
                    label="Inventory status",
                    value="Unknown" if active is None else "Active" if active else "Inactive",
                )
            )
            if row.get("description"):
                fields.append(
                    InventoryAttribute(label="Description", value=str(row["description"])[:4000])
                )
                if len(str(row["description"])) > 4000:
                    issue("Some circuit descriptions were shortened to fit the inventory view.")
        return fields

    selected = None
    parent = None
    listing = job.resource
    relations = []
    if job.record_id is not None:
        row = row_for(job.resource, decode(job.resource, job.record_id))
        current = ref(job.resource, row)
        parents = []
        cursor_kind, cursor_row = job.resource, row
        while cursor_kind in PARENTS:
            kind, field = PARENTS[cursor_kind]
            key = cursor_row.get(field)
            if key is None or not source.has_resource(kind):
                issue(
                    f"{kind.title()} ancestry is unavailable: its reference or mapping is missing."
                )
                break
            try:
                cursor_row = row_for(kind, str(key))
            except (ResourceNotFoundError, ConfigurationError, SourceDatabaseQueryError):
                issue(
                    f"{kind.title()} ancestry is unavailable: its reference or mapped query "
                    "could not be resolved."
                )
                break
            parents.insert(0, ref(kind, cursor_row))
            cursor_kind = kind
        selected = InventoryNode(
            **current.model_dump(),
            parents=parents,
            attributes=attributes(job.resource, row),
        )
        if job.resource in CHILDREN:
            parent, listing = current, CHILDREN[job.resource]
        if job.resource == "companies":
            relations = [
                InventoryRelation(label=label, resource=kind, parent=current)
                for label, kind in (("Billed circuits", "circuits"), ("Devices", "devices"))
            ]
    elif job.parent_id is not None and job.parent_resource is not None:
        parent = ref(
            job.parent_resource,
            row_for(job.parent_resource, decode(job.parent_resource, job.parent_id)),
        )
    filters: dict[str, object] = {}
    related_port = None
    if parent is not None:
        parent_source = decode(parent.resource, parent.id)
        if (parent.resource, listing) == ("ports", "circuits"):
            related_port = parent_source
        elif field := FILTERS.get((parent.resource, listing)):
            filters[field] = parent_source
        else:
            raise ConfigurationError("unsupported inventory parent relationship")
    if selected is not None and job.resource == "circuits":
        raise ConfigurationError("open circuits using the existing endpoint detail operation")
    if selected is not None and source.missing_fields_for(
        requirements={listing: ("source_id", *filters)}
    ):
        issue(f"Related {listing} cannot be listed because their mapping is incomplete.")
        rows = []
    else:
        try:
            rows = source.fetch_explorer_page(
                listing,
                filters=filters,
                query=job.query,
                related_port_id=related_port,
                offset=job.offset,
                limit=job.limit,
            )
        except (ConfigurationError, SourceDatabaseQueryError):
            if selected is None or listing == job.resource:
                raise
            issue(f"Related {listing} could not be read; the selected record is retained.")
            rows = []
    page_rows = rows[: job.limit]
    company_field = (
        "billing_company_id"
        if listing == "circuits"
        else "company_id"
        if listing == "racks"
        else None
    )
    companies = {}
    services = {}
    for resource, field, target in (
        ("companies", company_field, companies),
        ("services", "service_id" if listing == "circuits" else None, services),
    ):
        identifiers = list(
            dict.fromkeys(
                str(row[field]) for row in page_rows if field and row.get(field) is not None
            )
        )
        if not identifiers:
            continue
        if source.missing_fields_for(requirements={resource: ("source_id", "name")}):
            issue(
                f"{resource.title()} enrichment is unavailable because its mapping is incomplete."
            )
            continue
        try:
            related = source.fetch_by_ids(resource, identifiers)
        except (ConfigurationError, SourceDatabaseQueryError):
            issue(f"{resource.title()} enrichment could not be read.")
            continue
        if any(
            row.get("source_id") is None or not str(row["source_id"]).strip()
            for row in related
        ) or len({str(row["source_id"]) for row in related}) != len(related):
            raise UpstreamResponseError(
                "inventory enrichment identifiers must be unique and nonempty"
            )
        target.update((str(row["source_id"]), row) for row in related)
        if set(identifiers) - set(target):
            issue(f"Some referenced {resource} records are missing.")
    items = []
    for row in page_rows:
        fields = attributes(listing, row)
        service = services.get(str(row.get("service_id")))
        if service and service.get("name"):
            fields.append(InventoryAttribute(label="Service", value=str(service["name"])[:4000]))
            if len(str(service["name"])) > 4000:
                issue("Some service names were shortened to fit the inventory view.")
        elif service:
            issue("A referenced service name is unavailable.")
        company = companies.get(str(row.get(company_field))) if company_field else None
        items.append(
            InventoryNode(
                **ref(listing, row).model_dump(),
                attributes=fields,
                parents=[ref("companies", company)] if company else [],
            )
        )
    if len({str(row.get("source_id")) for row in rows}) != len(rows):
        raise UpstreamResponseError("inventory identifiers must be unique")
    # Re-read the selected/parent identity after the query, not a cached source record.
    for item in (selected, parent):
        if item is not None:
            row_for(item.resource, decode(item.resource, item.id))
    if require() != profile:
        raise ConfigurationError("source changed during the inventory read")
    return LiveResult(
        request_id=job.request_id,
        inventory=InventoryView(
            resource=job.resource,
            selected=selected,
            listing_resource=listing,
            items=items,
            parent=parent,
            next_offset=job.offset + job.limit if len(rows) > job.limit else None,
            relations=relations,
            issues=issues,
        ),
    )
