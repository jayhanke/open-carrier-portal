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
    "new_dark_fiber_circuits": ResourceDefinition(
        "Dark fiber circuits (optional)", ("source_id",), ("name",)
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
        "help": (
            "The service a partner can view. Map its stable ID, name and path reference. "
            "Service and description fields add detail. A/Z locations follow the path's "
            "endpoint ports, not separate circuit location assignments."
        ),
        "examples": (
            ("LitCircuit.LitCircuitID", "circuits.source_id"),
            ("LitCircuit.LitCircuitName", "circuits.name"),
            ("LitCircuit.CircuitLayoutID", "circuits.circuit_layout_id"),
            ("LitCircuit.ServiceID", "circuits.service_id"),
        ),
    },
    "devices": {
        "label": "Devices",
        "description": "Device identity, type, company, rack and monitoring linkage.",
        "resources": ("devices",),
        "help": (
            "Equipment that owns ports. devices.rack_id must reference the ID mapped under "
            "racks.source_id; the rack provides its physical location. Observium ID links "
            "the inventory device to monitoring, not to another inventory device."
        ),
        "examples": (
            ("Device.DeviceID", "devices.source_id"),
            ("Device.DeviceName", "devices.name"),
            ("Device.CompanyLocationID", "devices.rack_id"),
            ("Device.ObserviumID", "devices.observium_id"),
        ),
        "api_fields": ("monitoring_state", *CircuitDeviceStatus.model_fields),
        "api_note": "Hardware and current status come from the monitoring API, not SQL.",
    },
    "ports": {
        "label": "Ports / interfaces",
        "description": "Port identity, device, owner, connector and monitoring linkage.",
        "resources": ("ports",),
        "help": (
            "An interface belonging to a device. device_id references devices.source_id. "
            "Observium ID identifies that interface in monitoring; it is not the device ID. "
            "Current interface status comes from the API-supplied fields."
        ),
        "examples": (
            ("Port.PortID", "ports.source_id"),
            ("Port.PortName", "ports.name"),
            ("Port.DeviceID", "ports.device_id"),
            ("Port.ObserviumID", "ports.observium_id"),
        ),
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
        "help": (
            "A physical address shared by one or more racks or company placements. "
            "Keep street, city, floor, suite and coordinates here, not rack-unit positions. "
            "Its source_id is the value referenced by racks.location_id."
        ),
        "examples": (
            ("Location.LocationID", "locations.source_id"),
            ("Location.Address1", "locations.address1"),
            ("Location.City", "locations.city"),
            ("Location.Zip", "locations.postal_code"),
        ),
    },
    "companies": {
        "label": "Companies",
        "description": "Company identity for racks, devices and port ownership.",
        "resources": ("companies",),
        "help": (
            "Optional company names for rack ownership, devices and ports. Company references "
            "must match companies.source_id. A company-location ID is not a company ID."
        ),
        "examples": (
            ("company.Company_ID", "companies.source_id"),
            ("company.CompanyName", "companies.name"),
        ),
    },
    "racks": {
        "label": "Racks",
        "description": "Rack identity, owning company and physical location.",
        "resources": ("racks",),
        "help": (
            "The parent placement containing devices and optional rack positions. "
            "In the supplied schema CompanyLocation carries the company, physical-location "
            "link and rack name, so it maps here. RackUnit describes a position inside that "
            "placement and belongs under Rack positions instead. location_id references "
            "locations.source_id; company_id references companies.source_id."
        ),
        "examples": (
            ("CompanyLocation.CompanyLocationID", "racks.source_id"),
            ("CompanyLocation.RackName", "racks.name"),
            ("CompanyLocation.LocationID", "racks.location_id"),
            ("CompanyLocation.Company_ID", "racks.company_id"),
        ),
    },
    "rack_positions": {
        "label": "Rack positions",
        "description": "Optional device positions within a rack, such as U20.",
        "resources": ("rack_positions",),
        "help": (
            "Optional placement records for devices, such as U20 or U20-U21. This is the "
            "OCP target for the supplied RackUnit table, not the Racks target. Each record "
            "has its own position ID and label. device_id identifies the occupying device "
            "and is required when this section is mapped. rack_id identifies its parent "
            "rack, not a company or address. If omitted, the device still supplies its rack. "
            "A device can have several position records."
        ),
        "examples": (
            ("RackUnit.RackUnitID", "rack_positions.source_id"),
            ("RackUnit.RackUnitName", "rack_positions.name"),
            ("RackUnit.DeviceID", "rack_positions.device_id"),
            ("RackUnit.CompanyLocationID", "rack_positions.rack_id"),
        ),
    },
    "services": {
        "label": "Services",
        "description": "Service name, type and source bandwidth text.",
        "resources": ("services",),
        "help": (
            "Optional service details referenced by circuits.service_id. When mapped, "
            "provide the service ID, type and bandwidth. Bandwidth remains source text "
            "such as 10G; OCP does not guess units or convert it."
        ),
        "examples": (
            ("Service.ServiceID", "services.source_id"),
            ("Service.ServiceType", "services.type"),
            ("Service.ServiceBW", "services.bandwidth"),
        ),
    },
    "paths": {
        "label": "Circuit paths",
        "description": "Path identity and its A/Z endpoint ports.",
        "resources": ("circuit_layouts",),
        "help": (
            "The path container referenced by a circuit. A/Z port IDs reference "
            "ports.source_id and determine the circuit endpoints. This is not the ordered "
            "list of intermediate components; those belong under Circuit path steps."
        ),
        "examples": (
            ("CircuitLayout.CircuitLayoutID", "circuit_layouts.source_id"),
            ("CircuitLayout.PortA", "circuit_layouts.port_a_id"),
            ("CircuitLayout.PortZ", "circuit_layouts.port_z_id"),
        ),
    },
    "path_steps": {
        "label": "Circuit path steps",
        "description": "Ordered port, patch and pseudowire references within a path.",
        "resources": ("circuit_layout_items",),
        "help": (
            "Ordered components inside a circuit path. The path ID references "
            "circuit_layouts.source_id; position controls order. item_type must identify "
            "Port, Patch, PW or Pseudowire. item_source_id then references the ID in that "
            "component's mapping. Required for ordered-path sharing, not endpoint-only sharing."
        ),
        "examples": (
            ("CircuitLayoutItem.CircuitLayoutItemID", "circuit_layout_items.source_id"),
            ("CircuitLayoutItem.CircuitLayoutID", "circuit_layout_items.circuit_layout_id"),
            ("CircuitLayoutItem.ItemIndex", "circuit_layout_items.position"),
            ("CircuitLayoutItem.TableName", "circuit_layout_items.item_type"),
            ("CircuitLayoutItem.TablePK", "circuit_layout_items.item_source_id"),
        ),
    },
    "patches": {
        "label": "Patches",
        "description": "Patch type and primary or secondary endpoint ports.",
        "resources": ("patches",),
        "help": (
            "A physical connection used by a path step. Both primary endpoint IDs "
            "reference ports.source_id. Secondary endpoint ports are optional; NULL or 0 "
            "means no secondary port. Other IDs must resolve to existing ports. Map this "
            "section when shared paths contain Patch steps."
        ),
        "examples": (
            ("Patch.PatchID", "patches.source_id"),
            ("Patch.PortID_A", "patches.port_a_id"),
            ("Patch.PortID_B", "patches.port_b_id"),
        ),
    },
    "pseudowires": {
        "label": "Pseudowires",
        "description": "Pseudowire identity and endpoint ports.",
        "resources": ("pseudowires",),
        "help": (
            "A logical connection used by a PW or Pseudowire path step. Both endpoint "
            "IDs reference ports.source_id. source_id is the inventory record ID; "
            "pseudowire_id is the optional displayed PW identifier."
        ),
        "examples": (
            ("PseudoWire.PseudoWireID", "pseudowires.source_id"),
            ("PseudoWire.PWID", "pseudowires.pseudowire_id"),
            ("PseudoWire.PortAID", "pseudowires.port_a_id"),
            ("PseudoWire.PortBID", "pseudowires.port_b_id"),
        ),
    },
    "new_dark_fiber_circuits": {
        "label": "Dark fiber circuits",
        "description": "Dark-fiber segment descriptions within an approved circuit path.",
        "resources": ("new_dark_fiber_circuits",),
        "help": (
            "Optionally map this section to enrich NewDarkFiberCircuit steps with "
            "descriptions. Without it, live portal layouts retain the declared item label "
            "and mark its details unavailable. With it, the path shows the segment "
            "description, as in SFNCustomer; adjacent Port steps "
            "provide connected equipment and Observium status. This grants no separate "
            "dark-fiber inventory browsing or billing/order access."
        ),
        "examples": (
            ("NewDarkFiberCircuit.NewDarkFiberCircuitID", "new_dark_fiber_circuits.source_id"),
            ("NewDarkFiberCircuit.Description", "new_dark_fiber_circuits.name"),
        ),
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
            "new_dark_fiber_circuits",
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
    "new_dark_fiber_circuits": ("source_id", "name"),
}

SHARING_FIELDS = {
    resource: BASIC_FIELDS[resource]
    for resource in SOURCE_RESOURCES
    if resource != "rack_positions"
}
