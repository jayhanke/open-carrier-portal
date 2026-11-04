from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from open_carrier_portal.circuits import _active
from open_carrier_portal.company_policies import source_fingerprint
from open_carrier_portal.errors import (
    ConfigurationError,
    ResourceNotFoundError,
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

    def row_for(resource: InventoryResource, identifier: str) -> dict[str, Any]:
        rows = source.fetch_matching(resource, {"source_id": identifier}, limit=1)
        if len(rows) != 1:
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
            name = str(row.get("name") or resource.rstrip("s").title())
        return InventoryRef(
            resource=resource,
            id=repository.ocp_resource_id(namespace, KINDS[resource], str(key)),
            name=name or "Site",
        )

    def attributes(resource: InventoryResource, row: dict[str, Any]) -> list[InventoryAttribute]:
        fields = [
            InventoryAttribute(label=field.replace("_", " ").title(), value=str(row[field]))
            for field in ATTRIBUTES[resource]
            if row.get(field) is not None
        ]
        if resource == "circuits":
            active = _active(row.get("active"))
            fields.append(
                InventoryAttribute(
                    label="Inventory status",
                    value="Unknown" if active is None else "Active" if active else "Inactive",
                )
            )
            if row.get("description"):
                fields.append(
                    InventoryAttribute(label="Description", value=str(row["description"]))
                )
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
            if key is None:
                raise UpstreamResponseError("inventory parent reference is missing")
            cursor_row = row_for(kind, str(key))
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
    rows = source.fetch_explorer_page(
        listing,
        filters=filters,
        query=job.query,
        related_port_id=related_port,
        offset=job.offset,
        limit=job.limit,
    )
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
        if identifiers and source.has_resource(resource):
            target.update(
                (str(row["source_id"]), row) for row in source.fetch_by_ids(resource, identifiers)
            )
    items = []
    for row in page_rows:
        fields = attributes(listing, row)
        service = services.get(str(row.get("service_id")))
        if service and service.get("name"):
            fields.append(InventoryAttribute(label="Service", value=str(service["name"])))
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
        ),
    )
