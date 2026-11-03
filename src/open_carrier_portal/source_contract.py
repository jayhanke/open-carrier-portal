from dataclasses import dataclass

from open_carrier_portal.models import CircuitDeviceStatus, CircuitPortStatus


@dataclass(frozen=True, slots=True)
class ResourceDefinition:
    label: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    private: tuple[str, ...] = ()
    legacy: tuple[str, ...] = ()

    @property
    def fields(self) -> tuple[str, ...]:
        return self.required + self.optional + self.private


SOURCE_RESOURCES = {
    "companies": ResourceDefinition("Company (optional)", ("source_id", "name")),
    "locations": ResourceDefinition(
        "Locations",
        ("source_id",),
        (
            "address1",
            "address2",
            "city",
            "state",
            "postal_code",
            "latitude",
            "longitude",
            "floor",
            "suite",
        ),
    ),
    "racks": ResourceDefinition(
        "Racks",
        ("source_id", "name", "location_id"),
        ("company_id", "site_code"),
        legacy=("rack_name",),
    ),
    "rack_positions": ResourceDefinition(
        "Rack positions (optional)",
        ("source_id", "name", "device_id"),
        ("rack_id",),
    ),
    "devices": ResourceDefinition(
        "Devices",
        ("source_id", "name", "rack_id"),
        ("company_id", "type", "observium_id"),
        private=("management_ip",),
    ),
    "ports": ResourceDefinition(
        "Ports",
        ("source_id", "name", "device_id"),
        (
            "owner_company_id",
            "connector_type",
            "observium_id",
            "nni",
            "parent_port_id",
            "vlan",
            "card_type_id",
        ),
        private=("ip_address",),
    ),
    "circuits": ResourceDefinition(
        "Circuit information",
        (
            "source_id",
            "name",
            "circuit_layout_id",
        ),
        (
            "billing_company_id",
            "service_id",
            "description",
        ),
        private=("service_order_id",),
        legacy=("a_company_location_id", "z_company_location_id", "active"),
    ),
    "services": ResourceDefinition(
        "Service and bandwidth (optional)", ("source_id", "type", "bandwidth"), ("name",)
    ),
    "circuit_layouts": ResourceDefinition(
        "Endpoints",
        ("source_id", "port_a_id", "port_z_id"),
        ("name",),
    ),
    "circuit_layout_items": ResourceDefinition(
        "Route steps (optional)",
        ("source_id", "circuit_layout_id", "position", "item_type", "item_source_id"),
    ),
    "patches": ResourceDefinition(
        "Patches (optional)",
        ("source_id", "port_a_id", "port_b_id"),
        (
            "patch_type",
            "port_a_secondary_id",
            "port_b_secondary_id",
            "dark_fiber_circuit_id",
            "order_date",
            "install_date",
            "cancel_date",
            "removal_date",
            "needs_removal",
            "length",
        ),
    ),
    "pseudowires": ResourceDefinition(
        "Pseudowires (optional)",
        ("source_id", "port_a_id", "port_b_id"),
        ("pseudowire_id",),
    ),
}

CORE_RESOURCES = (
    "circuits",
    "circuit_layouts",
    "ports",
    "devices",
    "racks",
    "locations",
)
MONITORING_FIELDS = {"devices": ("observium_id",), "ports": ("observium_id",)}

OCP_TABLES = {
    "circuits": {
        "label": "Circuits",
        "description": (
            "Circuit identity, service and path linkage. Locations follow endpoint devices."
        ),
        "resources": ("circuits",),
    },
    "devices": {
        "label": "Devices",
        "description": "Device identity, type, company, rack and monitoring linkage.",
        "resources": ("devices",),
        "api_fields": ("monitoring_state", *CircuitDeviceStatus.model_fields),
        "api_note": "Hardware and current status come from the monitoring API, not SQL.",
    },
    "ports": {
        "label": "Ports / interfaces",
        "description": "Port identity, device, owner, connector and monitoring linkage.",
        "resources": ("ports",),
        "api_fields": ("monitoring_state", *CircuitPortStatus.model_fields),
        "api_note": (
            "Interface status comes from the monitoring API. Optical levels stay empty "
            "when not reported; optical_state is calculated by OCP."
        ),
    },
    "locations": {
        "label": "Locations",
        "description": "Physical address, floor, suite and coordinates.",
        "resources": ("locations",),
    },
    "companies": {
        "label": "Companies",
        "description": "Company identity for racks, devices and port ownership.",
        "resources": ("companies",),
    },
    "racks": {
        "label": "Racks",
        "description": "Rack identity, owning company and physical location.",
        "resources": ("racks",),
    },
    "rack_positions": {
        "label": "Rack positions",
        "description": "Optional device positions within a rack, such as U20.",
        "resources": ("rack_positions",),
    },
    "services": {
        "label": "Services",
        "description": "Service name, type and source bandwidth text.",
        "resources": ("services",),
    },
    "paths": {
        "label": "Circuit paths",
        "description": "Path identity and its A/Z endpoint ports.",
        "resources": ("circuit_layouts",),
    },
    "path_steps": {
        "label": "Circuit path steps",
        "description": "Ordered port, patch and pseudowire references within a path.",
        "resources": ("circuit_layout_items",),
    },
    "patches": {
        "label": "Patches",
        "description": "Patch type and primary or secondary endpoint ports.",
        "resources": ("patches",),
    },
    "pseudowires": {
        "label": "Pseudowires",
        "description": "Pseudowire identity and endpoint ports.",
        "resources": ("pseudowires",),
    },
}

MAPPING_GROUPS = {
    "location": {
        "label": "Location",
        "description": "Device racks, their physical addresses and optional rack positions.",
        "resources": ("racks", "locations", "companies", "rack_positions"),
    },
    "circuit": {
        "label": "Circuit",
        "description": "The service being shared: its identifier, name, description and bandwidth.",
        "resources": ("circuits", "services"),
    },
    "path": {
        "label": "Circuit path",
        "description": (
            "A/Z endpoints and optional ordered steps, with port and device information."
        ),
        "resources": (
            "circuit_layouts",
            "circuit_layout_items",
            "ports",
            "devices",
            "patches",
            "pseudowires",
        ),
    },
}

BASIC_FIELDS = {
    "companies": ("source_id", "name"),
    "locations": (
        "source_id",
        "address1",
        "address2",
        "city",
        "state",
        "postal_code",
        "latitude",
        "longitude",
        "floor",
        "suite",
    ),
    "racks": (
        "source_id",
        "name",
        "location_id",
        "company_id",
    ),
    "rack_positions": ("source_id", "name", "device_id", "rack_id"),
    "devices": ("source_id", "name", "rack_id", "company_id", "type", "observium_id"),
    "ports": (
        "source_id",
        "name",
        "device_id",
        "owner_company_id",
        "connector_type",
        "observium_id",
    ),
    "circuits": (
        "source_id",
        "name",
        "description",
        "service_id",
        "circuit_layout_id",
    ),
    "services": ("source_id", "name", "type", "bandwidth"),
    "circuit_layouts": ("source_id", "port_a_id", "port_z_id"),
    "circuit_layout_items": (
        "source_id",
        "circuit_layout_id",
        "position",
        "item_type",
        "item_source_id",
    ),
    "patches": (
        "source_id",
        "patch_type",
        "port_a_id",
        "port_b_id",
        "port_a_secondary_id",
        "port_b_secondary_id",
    ),
    "pseudowires": ("source_id", "pseudowire_id", "port_a_id", "port_b_id"),
}

SHARING_FIELDS = {
    resource: BASIC_FIELDS[resource]
    for resource in SOURCE_RESOURCES
    if resource != "rack_positions"
}
