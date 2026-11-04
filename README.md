# open-carrier-portal
Open carrier portal with API

The purpose of Open Carrier Portal [OCP] is to facilite the sharing of information between Internet Service Providers, Internet Exchanges, Data Centers and Transport Providers.

The implementation will primarily consist of a rest API as well as a reference implementation. The reference implementation will be targeted at smaller carriers and data centers.

Network Information Functionality

For instance, a data center could date one of their customer a particular CFA space for the purpose of issueing LOAs. The carrier, in their own portal, could issue a LOA/CFA and notify both the data center and the other party of the availablity of the allocated port. The data center could optionally issue paperwork for the ordering party to place a cross connect order. Following completion, that port status could be available for either party to query. So during turn up the installing party could verify the light levels at the far end receiver without contacting a NOC. Further, the data center operator could automatically get a notification that the polairty and loss reading are within the expected ranges.

A party can choose what information to share. For instance, an IXP could choose to share port status such as alarms, light levels or traffic stats. A party could also offer the functionality to open tickets for issues.

Social Network Functionality

In the portal, carriers would have the ability to establish a relationship with other entities and then restrict their communications to those entities. The relationships are bilateral only and require mutual consent. Parties outside the system could fall back to industry standard emails. The relationships would be between companies.

Tax/Compliance Functionality

The portal could also be used to exchange (and verify) compliance information such as 499 status in the US.

Maintenance/Outage Notifications

Maintenance notifications listing service affecting issues. Optionally, these notifications could be passed on to affected customers.

Escalation List Contacts

Carriers can provide an up-to-date escalation list for issue resolution.


Desired integrations

IXP Manager
FullCTL
Observium
LibreNMS
PeeringDB
NetBox
Docusign/AdobeSign etc.

## Current connector foundation

The first implementation is a self-hosted FastAPI connector for Observium. Each company
runs its own connector:

1. A partner company authenticates to the connector with an OAuth 2.0
   client-credentials client.
2. The connector verifies the client, requested scope, active bilateral relationship,
   and resource grant.
3. The connector uses its own private, read-only API token to query the local Observium
   installation.
4. Observium data is mapped to stable OCP models before it is returned. Partners never
   receive Observium credentials or unrestricted Observium responses.

The base objects are connector identity, partner organization, OAuth client, bilateral
relationship, resource grant, integration configuration, audit event, and a generic
monitoring adapter. The adapter boundary allows another monitoring system to implement
the same device, interface, alarm, capability, and metric contracts later.

Implemented partner routes:

- `POST /oauth/token`
- `GET /v1/capabilities`
- `GET /v1/devices` and `GET /v1/devices/{device_id}`
- `GET /v1/interfaces` and `GET /v1/interfaces/{interface_id}`
- `GET /v1/alarms`
- `GET /v1/metrics`
- `GET /v1/circuits/{circuit_id}`
- `GET /v1/circuits/{circuit_id}/path`
- `GET /health/live` and `GET /health/ready`

Resource IDs are opaque, list results are filtered by the partner's grant, pagination
cursors are signed and partner-bound, and access decisions are audited in the local
SQLite metadata database. Operational network data remains in Observium and NetTool.
Circuit path access additionally requires an active `circuits:read` grant and an
explicit active share for that partner; a circuit share alone never exposes other
circuit records or independent path-component routes.

### Read-only circuit-sharing v1

The connector keeps identity, partners, relationships, OAuth clients, grants, circuit
shares, opaque resource-ID mappings, source configuration and audit records locally.
Local administration also needs its own accounts, sessions and settings. It does not
copy operational companies, sites, devices, ports, circuits or telemetry into that
database. Source records and monitoring results are read on demand; enrichment caches
exist only within a single request.

`GET /v1/circuits/{circuit_id}` returns the opaque circuit ID, name, description,
service name/type, bandwidth, legacy active state, A/Z rack/location context and the
visible ordered path. Bandwidth preserves `Service.ServiceBW` as text; no units are assumed.
The mapper exposes the full supported OCP target catalog, not just three broad groups.
The source `CompanyLocation` represents an OCP **Rack**; `Location` represents its
physical address, and `RackUnit` represents optional device positions such as U20/U21.
The relationship is port → device → rack → location. Multiple racks can share one
physical address. Rack names use `CompanyLocation.RackName` in the example mapping.
If rack positions are mapped, devices expose all matching positions, not an arbitrary
first matching row. Existing API response names (`site`, `rack_units`, `a_location`,
`z_location`) and opaque ID namespaces are retained for compatibility.

Path ports include device, company, rack, physical address and rack-position details, plus
current monitoring linked by source `Device.ObserviumID` and `Port.ObserviumID`.
Sharing a circuit includes monitoring for its visible components; separate grants
for arbitrary Observium resources are not required for this parent-authorized read.
Independent device/interface routes still require their own scopes and grants.
Existing visibility restrictions remain: `summary` returns no path; `endpoints`
returns endpoint ports; path modes return ordered components and their endpoint ports.
Legacy `metrics` shares return summary only; this does not enable historical telemetry.
Revoked, unshared and unauthorized circuit IDs are not disclosed.

Monitoring states distinguish `available`, `not_linked` and `not_configured`.
Configured-upstream errors are explicit failed responses and audited, not empty
successes. Port status includes interface name/description, speed, administrative
and operational status and last poll time. Optical fields are nullable, with
`optical_state=not_reported` when the adapter supplies neither value. The current
Observium adapter does not yet resolve optical sensors: their payload, port association
and direction must be verified rather than guessed from sensor descriptions.

Public circuit responses whitelist fields; they never return source management IPs,
port IP addresses, MACs, asset IDs, service-order references, credentials or raw source
records. Private mapping fields may be inspected by authenticated local administrators,
but v1 has no policy switch to publish them. No new child-resource enumeration routes
are introduced by circuit sharing.

Mapping targets come from a separate OCP field catalog, not a copy of the source
schema. The mapping board shows connected database tables on the left and twelve
OCP target tables on the right: **Circuits, Devices, Ports/interfaces, Locations,
Companies, Racks, Rack positions, Services, Circuit paths, Circuit path steps, Patches
and Pseudowires**. Search the right-hand catalog by table or field.
Each OCP table has a **?** button with its purpose, relationship meanings and example
source-column assignments. Open it by mouse or keyboard; close with **Close**, Escape
or a click outside. Opening help does not select a source section or change a draft.
Examples illustrate the supplied schema, not mandatory customer table names.
For example, `RackUnit` maps to **Rack positions**: `RackUnitID` to `source_id`,
`RackUnitName` to `name`, `DeviceID` to `device_id`, and `CompanyLocationID` to `rack_id`.
The last field points to the parent rack, not a company or physical address.
Locations contain only address, floor, suite and coordinate fields; rack identity
and company/location links belong to Racks. New connections start with empty mapping
slots, not example source assignments. Each database-mapped OCP row has a customer
drop box on the left and its fixed logical OCP field name on the right, for example
**[Drop customer field here] → devices.name**. After assigning a source, the left box
shows that source's table and column; the OCP target never changes. Drag a source
column into that box, or select a column and choose **Assign** beside the target.
Dropping into an unconfigured section selects that column's table as its primary
source; an additional table still needs a confirmed join. Select an OCP section
heading to choose which section to preview or configure.
Device/port hardware and monitoring fields appear on their respective cards, clearly
marked **API supplied** and without database drop boxes. Alarms and Metrics are not
shown in this inventory mapper; their separate APIs and authorization are unchanged.
These fields do not add permissions, adapter capabilities or historical telemetry to
circuit shares. Database mappings supply circuit inventory/path enrichment; independent
device/interface endpoints remain monitoring-backed.
Essential relationships are visible beside the display fields, not hidden under
advanced settings: port-to-device, device-to-rack, rack-to-location, circuit-to-path
and endpoint ports. Device and port Observium IDs are visible database
mapping targets; current status and hardware remain API supplied. Additional
compatibility/private fields and source joins are advanced. Joins must be confirmed,
never inferred from similar column names.

Only map the sections needed for the chosen sharing detail. A summary needs circuit
ID/name. Endpoint sharing needs the complete circuit-to-path-to-port-to-device-to-rack-
to-location graph. The six core mapping sections are circuits, circuit paths, ports,
devices, racks and locations. These are logical targets: a customer can map several
sections to the same source table rather than duplicate their database. Ordered-path sharing also
needs ordered route steps, plus patch/pseudowire mappings when those step types occur.
Primary endpoint references cannot be NULL; an empty ordered path or missing device,
rack or location record fails explicitly instead of returning a successful partial path.
Companies, rack positions and service enrichment remain optional. If a service is
mapped, map its type and bandwidth as well as its ID. Unconfigured optional enrichment referenced
by a circuit is listed in `omitted_enrichment`; incomplete configured mappings,
broken references and upstream failures fail explicitly. Optional source values can
be NULL.

Circuits no longer offer separate A/Z location or Active mapping rows. Path-bearing
responses derive A/Z context from `circuit_layouts.port_a_id` / `port_z_id`, following
each port's device and rack; they do not guess endpoints from ordered-step position.
The API retains nullable `active`, `a_location` and `z_location` fields. Previously
configured Active mappings still supply that value; new mappings return `active=null`.
Summary-only mappings need no path graph and can leave location fields NULL; existing
summary location assignments remain readable for compatibility.

Monitoring is optional. When configured, both `devices.observium_id` and
`ports.observium_id` mapping keys are required for path-bearing views. NULL values
in those mapped columns mean genuinely unmonitored inventory and are allowed.
A monitored port must have a monitored parent device, and the monitoring API's
port/device relationship must agree with the mapped inventory. Monitoring IDs remain
internal; partners receive opaque public IDs.

The page and `ocp test-source` check the six core relationship sections plus enabled
sections, reporting missing fields. Each API response also enforces the mappings
needed by its share's visibility. `mapping_state` describes that mapping check, not
whether every optional enrichment section has been enabled.
Existing mappings are preserved. Older profiles missing these relationships must
be completed before sharing endpoint or ordered-path details; summary-only shares
can still use just the circuit ID/name mapping.
Legacy mapping resource names `company_locations` and `rack_units` load as `racks`
and `rack_positions`; device/position `company_location_id` loads as `rack_id`.
Loading a profile does not rewrite its stored JSON. An explicit save uses the current
names, preserves joins and retained legacy circuit fields, and rejects conflicting
legacy/current definitions rather than choosing one silently.
Use **Load example mappings as draft** under **Source configuration (advanced)** only
if you intentionally want the supplied example assignments. Review every table/column
assignment, then validate and save. The focused example template excludes
private fields, billing/order references and unused source metadata; the advanced
catalog remains available for existing mappings. Saved mappings and public ID
namespaces are preserved, not automatically replaced.

Service orders, billing workflows, tickets, LOAs, historical metrics and partner-submitted
changes remain outside this read-only v1. Hosted-portal pairing still provides only
registration and heartbeats; it does not automatically retrieve this circuit response.

### Outbound hosted-portal connection

The separate hosted OpenPortal application supports organization-admin pairing
and outbound connection status. No inbound connector port is required for this
connection. It currently carries **registration and heartbeats only**, not resource
retrieval, remote commands, or synchronization of relationship/share grants.
Partner API authorization remains unchanged.

In the hosted portal, sign in as your **organization admin**, open **Connector**,
and generate a pairing code. Platform owners create client organizations but do
not implicitly become members of them.

In this connector's installed environment with its usual `OCP_*` configuration:

```powershell
# Only if this connector has not already been initialized:
ocp init --organization-name "Your Company"

ocp pair-portal --portal-url http://127.0.0.1:8080
# Paste the one-time code at the private prompt.
ocp run-portal-agent
```

Use the portal's reachable HTTPS origin in production. Localhost HTTP is allowed
only for development on the same machine. The agent verifies TLS and does not
follow redirects or use environment-provided proxies. The portal's organization
association comes from the pairing code, not a user-supplied tenant ID; pairing
does not rewrite the connector's existing local identity or partner configuration.

The pairing command stores a bearer credential in
`~/.open-carrier-portal/portal-connection.json` and refuses to overwrite an existing
file. On Unix it creates the directory with mode 700 and file with mode 600; on
Windows keep this file in your private user profile and restrict its ACL to the
service account. Do not commit, share, or log this file. Both commands accept
`--connection-file` for a service-account-specific location. To replace a pairing,
generate a new code in the portal and use a new private file path with **both**
commands, then securely remove the old credential file.

Run the agent under your service supervisor for persistent operation. It sends
a heartbeat every 30 seconds, reports network/server errors, and retries those
errors at that interval. Invalid/revoked credentials stop the agent with an error
instead of silently reconnecting. `ocp run-portal-agent --once` sends a single
heartbeat for troubleshooting. Stop an interactive agent with Ctrl+C.
Refresh the portal page for current status; **Connected** means a heartbeat was
received within 90 seconds, not that Observium or the source database is healthy.
Use the portal's **Disconnect connector** action to revoke the credential.

## Connector administration web UI

This is a **local UI for the carrier connector**, not the separately deployed
Carrier-Portal hosted application. It works without the hosted portal. **One connector
process serves both the partner API and the authenticated UI on the same port.**

### First start

Use this connector's Python **3.14+** virtual environment, with its usual private
`OCP_TOKEN_SIGNING_KEY`, `OCP_DATABASE_PATH`, issuer, and source credentials loaded.
The hosted portal's Python environment and `open-portal` accounts are not used here.

```bash
# Run inside the connector checkout, with its virtual environment active:
ocp create-admin --username admin
ocp serve
```

Choose a private password of 10-256 characters at the terminal prompts. There is no
default account, public account registration, or web-based first-admin bootstrap.
Open **http://127.0.0.1:8000/admin/setup**. The wizard checklist guides you through
company identity, optional portal pairing, source connections, mapping validation,
and sharing review. Existing `ocp init` identities and source profiles are reused.
Changing the company name preserves organization and connector IDs.

For a connector on another server, use an SSH tunnel from your workstation:

```bash
ssh -L 8000:127.0.0.1:8000 YOUR_USER@CONNECTOR_SERVER
```

Then browse the same localhost URL. The connector CLI refuses non-loopback
bind addresses by default. For an HTTPS reverse proxy, pass `--base-url https://YOUR_ADMIN_HOSTNAME` while
keeping the listener loopback. The proxy must preserve that Host header and
restrict `/admin` and `/admin/*` to authorized operators; publish only the required
API routes to partners. With a different port, supply matching
`--port` and `--base-url` values.

Use one `ocp-connector` systemd service running `ocp serve`; no `ocp-admin` service is
needed. The connector starts the portal heartbeat task and stops it on shutdown.
Do not run multiple workers or a separate portal agent for the same pairing file.
The legacy `ocp serve-admin` command now starts the same combined application,
also on port 8000 by default.

For temporary testing on a trusted private network only, explicitly set
`OCP_ALLOW_INSECURE_HTTP=true` in the connector environment. This permits literal
RFC1918/ULA private IPs, not public IPs or arbitrary HTTP hostnames. For example:

```ini
OCP_ALLOW_INSECURE_HTTP=true
OCP_ISSUER_URL=http://10.210.4.223:8000
OCP_ADMIN_BASE_URL=http://10.210.4.223:8000
```

Start with `ocp serve --host 10.210.4.223` and open
`http://10.210.4.223:8000/admin/setup`. The bind IP must match the configured UI
origin; wildcard network binds are not allowed. A warning is logged: admin
passwords, session cookies, partner secrets and data travel without encryption.
Restrict the listener to authorized test clients using your firewall. Disable
the option and return to a loopback listener behind HTTPS after testing.
This option does not relax TLS requirements for Observium, outbound partner
connections, or hosted-portal pairing.

Missing/disabled source profiles, invalid mappings, or missing saved integration
credentials are logged and shown to signed-in administrators. Integration endpoints
and readiness return 503 while configuration is invalid; the UI remains available
for repairs. Core configuration errors such as a missing signing key still prevent
startup. Use the same existing absolute `OCP_DATABASE_PATH` and secrets when
switching from the old two-process setup.
Set a private umask (for example `umask 077`) before first initialization and protect
the metadata database and its backups: they contain local admin credential hashes,
session hashes, partner grants, configuration and audit events.

### Administration sections

- **Setup:** one numbered checklist for company identity, connections, database
  mapping and optional Carrier Portal pairing, followed by verification and sharing last.
  Connections links to database/monitoring setup and partner connections so the
  checklist stays compact as more connections are added. Database badges
  distinguish selected, missing and disabled profiles; selecting a source does not
  mark it healthy. Run the explicit source connectivity/mapping check before sharing.
  Portal pairing is optional; a heartbeat does not mean source data is ready.
- **Identity:** initialize or rename your company without rotating identity IDs.
- **Carrier Portal:** a guided page opens the portal's **Connector** page in a new
  tab, explains how a company administrator generates a one-time code (valid for
  15 minutes), and accepts only that code. It warns that generating a replacement
  code disconnects the previous connector. The installer sets `OCP_PORTAL_URL`
  to the hosted portal's HTTPS origin and optionally `OCP_PORTAL_CA_BUNDLE` to a
  trusted PEM CA file, then restarts the connector. These are not editable form
  fields; submitted URL/CA fields cannot override installer settings. Without a
  configured portal address or an enabled company identity, the page explains what
  is missing instead of accepting a code. Existing pairings continue to use their
  saved portal address; existing saved CA settings remain a fallback if no CA is
  configured in the environment. Certificate verification remains mandatory.
  The pairing
  credential is saved locally using the existing protected connection file, never
  rendered back into a page. While the connector runs it sends heartbeats every
  30 seconds. The page distinguishes pairing from a confirmed connection and shows
  connection errors. Use **Check connection** to verify immediately. Do not run another
  `run-portal-agent` against the same connection file simultaneously.
  **Disconnect locally** deletes the saved credential; also revoke the connection
  in the hosted portal to invalidate the server-side credential. Disconnect before
  replacing a pairing. `serve --connection-file PATH` selects an existing
  CLI pairing file or a service-account-specific private location.
- **Connections:** a compact database card contains database name, host, port,
  username and password. Driver, connection name, password environment variable,
  TLS CA and profile status are under collapsed **Advanced settings**. Mapping,
  activation and optional monitoring have their own collapsed sections.
  **Test connection** tests the submitted details without saving them or checking
  mappings; re-enter an unsaved password when returning to the form. **Save connection**
  validates configuration and saves it without claiming connectivity or read-only
  permissions. Blank passwords retain the existing saved credential.
  If the configured password environment variable has a value, the page says
  **Password managed by server configuration**, hides the password input, and
  rejects UI attempts to override it. Otherwise UI passwords are saved in private
  files alongside the metadata database, in `<metadata-filename>.credentials/`,
  not inside SQLite, the environment file, logs or rendered pages. Environment
  credentials take precedence. API startup, CLI source testing and UI mapping/
  connection checks all load the same credentials. Keep these files with protected
  database backups. Unix directories/files use modes 700/600; on Windows restrict
  the directory ACL to the connector service account. These files contain plaintext
  secrets protected by filesystem permissions, not an encrypted vault.
  Use absolute paths for SQLite. Remote MySQL requires a trusted TLS CA; existing
  CLI plaintext opt-ins are preserved but cannot be newly enabled through the UI.
  Connectivity/mapping tests are SELECT-only and **cannot prove** a MySQL account
  has no write privileges; provision a dedicated read-only account yourself.
  For optional Observium monitoring, set the actual read-only API token in the
  **connector service's environment**, normally as `OCP_OBSERVIUM_API_TOKEN`.
  Connections accepts the **environment-variable NAME**, never the token itself.
  The form saves only the HTTPS URL, variable name and optional CA path;
  Observium token entry and local token-file storage are not supported.
  For a manual Linux launch, run `export OCP_OBSERVIUM_API_TOKEN='<your-read-only-api-token>'`
  in the same shell before starting the connector. For systemd or Docker, configure
  the service/container environment and restart it. Setting the variable in another
  terminal does not update a running process. The app does not automatically load
  `.env` files; the deployment must load them. Protect any environment files and
  exclude secrets from source control.
  Save the URL and variable name, then use **Test effective Observium connection**.
  Explicit `OCP_OBSERVIUM_BASE_URL`/`OCP_OBSERVIUM_API_TOKEN` deployment settings
  override saved UI settings, and the test checks those effective settings.
  Restart after saving to apply changes to the partner API.
  Secrets remain on this connector, never in the hosted portal.
- **Mapping:** browse/filter your database schema, choose a connector resource and
  source table, then drag columns onto connector fields. Keyboard users can select
  a column and choose **Assign** instead. Schema discovery returns identifiers, not
  records, up to 50,000 columns and 50,000 foreign-key columns. Larger databases
  fail explicitly without truncating the schema; expose a dedicated read-only schema
  containing the tables needed for sharing if those bounds are exceeded.
  Collapsed source tables create their column controls only when expanded or found
  by search, keeping large catalogs usable. Declared single-column foreign keys suggest joins; explicitly confirm
  each suggestion or select a relationship manually. Similar column names are
  never treated as a relationship automatically.
  Up to eight joins per resource are supported. Each target must have a
  single-column primary or full, non-partial unique key; one-to-many and composite
  joins are not supported because they could multiply resource rows. Map child
  collections as separate resources instead. **LEFT JOIN** preserves unmatched
  source rows with NULL fields; **INNER JOIN** excludes them. The required
  `source_id` must stay on the primary source table.
  Preview reads at most five rows of explicitly mapped columns and shows the
  generated, parameterized SQL. Preview does not save the draft. **Validate and
  save mappings** checks every enabled resource and join before persisting;
  invalid mappings are not saved. Clear individual fields or remove a resource
  explicitly. Advanced JSON remains available; legacy single-table mappings are
  compatible. There is no arbitrary SQL editor and no writes to the source
  database. Validation checks schema and join keys, not complete source-data
  referential integrity or read-only permissions. Restart the connector after
  saving to apply mappings to the running API.
- **Partners:** provision direct inbound partners after confirming bilateral
  approval out of band. Select scopes and specific opaque resource IDs, or
  explicitly opt in to all resources for those scopes. Client secrets appear
  **once**, only in the successful creation response. Copy them privately;
  diagnostics and subsequent pages do not reveal stored secrets/hashes.
  Circuit shares additionally require a matching `circuits:read` grant. Revoking
  a partner disables its relationship, OAuth clients and circuit shares; already
  issued tokens then fail the API's existing live authorization checks.
- **Partner connections:** configure named outbound connections to other companies' connectors,
  using OAuth credentials they issue to you. Each secret stays in a named local
  environment variable. Test token acquisition and capabilities, or retrieve an
  explicitly authorized opaque circuit path. The connector's capabilities endpoint
  requires `observium:devices:read` and an available monitoring adapter; circuits-only
  connections report OAuth authentication separately without requesting capabilities.
  HTTPS is required outside localhost;
  TLS verification cannot be disabled, redirects are rejected, and tokens are
  neither persisted nor displayed. This is manual browsing, not background
  synchronization. Saving a new connection name preserves other saved connections.
- **Diagnostics:** Observium connection logs plus a collapsible view of all recent
  operational events, alongside the existing administration/partner API audit trails.
  Operational logs cover startup/configuration, HTTP request outcomes, administrator
  actions/session failures, database connectivity/mapping errors, Observium HTTP and
  transport failures, portal pairing/heartbeats and CLI outcomes. The newest 100 events
  per view are displayed; the newest 2,000 operational events are retained in the
  connector metadata database. This does not prune the existing audit trails.
  Each HTTP response includes a generated `X-Request-ID`; error pages show this
  diagnostic reference, which ties request and integration events together.
  Existing `X-Correlation-ID` API behavior remains separate and unchanged.
  Tokens, passwords, cookies, pairing codes, query strings, SQL/parameters and
  source/response records are not included in operational logs. Upstream failures
  record safe categories, HTTP status and target origin without URL credentials
  or paths. Unhandled server errors retain stack locations, not raw exception
  messages or payloads. Raw Uvicorn access logs are replaced with safe route-template
  request logs.
  Events are also written to service stderr: use `journalctl -u your-service-name -f`
  for systemd or `docker logs --tail 100 -f your-container-name` for Docker.
  Configure long-term collection/retention in your deployment. If local diagnostics
  persistence fails, `diagnostics.storage_failed` is reported in service output
  rather than silently hiding the logging failure.
  For an Observium failure, check **Observium connection logs** first: missing
  setup/token, expired admin form verification, DNS/TLS/timeout, rejected credentials,
  HTTP 404/429/5xx, redirects and invalid JSON are distinguished. No raw
  configuration/secret bundle is exported.
  Observium redirects do not pass a health check, even if they contain success-shaped
  JSON; configure the final HTTPS installation base URL instead.
  The read-only Observium health check requests `api/v0/devices/` with `pageno=1`
  and `pagesize=1`, and validates the device collection (an empty collection is valid).
  It does not depend on the installation serving an OpenAPI specification. Returned
  device records are not logged or persisted by this check.
- **Account:** change your password and invalidate that account's admin sessions.
  Additional local admins and forgotten-password recovery require CLI operator
  access:

  ```bash
  ocp create-admin --username another-admin
  ocp reset-admin-password --username admin
  ```

Create/reset commands report the resolved connector metadata database path.
Use the running connector service's environment, service user and working directory
so these commands update the same database as the web UI. A successful reset takes
effect without restarting the connector. Existing login-attempt throttling still
applies; wait five minutes if the browser reports too many attempts.

Admin sessions expire after **one hour of inactivity**, not one hour after login.
Authenticated page navigation and CSRF-validated actions renew both the server
expiry and cookie. Signed-in pages also report real keyboard, pointer, scroll and
drag/drop activity at most once per minute, so editing mappings without saving
counts as activity. Hidden or untouched tabs do not send periodic keepalive requests;
asset loads and status checks do not extend a session.

The page warns five minutes before inactivity expiry. On expiry, it keeps the page
and in-memory mapping draft rather than redirecting or clearing it. Sign in in a
separate tab, then choose **Resume session** on the original page. Resume verifies
the same administrator and refreshes CSRF values without replacing draft assignments
or previews. Known-expired forms are blocked from submitting until resumed.
Drafts are not persisted by this mechanism: closing or reloading a page can still
discard unsaved edits. Network renewal failures are shown explicitly and retried.

Sessions are stored server-side as token hashes, and
are separate from partner OAuth credentials. State-changing forms require CSRF
tokens. The UI enforces its configured host, checks supplied Origin headers, and
uses HttpOnly/SameSite=Strict cookies (Secure cookies on HTTPS). Failed logins are
rate-limited by client address. Password resets revoke existing admin sessions.

### Applying source changes and limitations

After validating a source profile, use **Select for API restart**. Restart the
connector service to apply the selection and any Observium changes. Saved UI
settings are read at API startup. Explicit environment settings
`OCP_SOURCE_DB_PROFILE` and `OCP_OBSERVIUM_BASE_URL` take precedence. Changes are not silently
hot-reloaded into existing API workers.

The existing Uvicorn factory entry point also serves both API and UI.
With that entry point, `OCP_ADMIN_BASE_URL` sets the UI's trusted origin (default:
`OCP_ISSUER_URL`) and `OCP_PORTAL_CONNECTION_FILE` can select a private pairing
file. `ocp serve` defaults its UI origin to `http://127.0.0.1:8000`, or the chosen
port; `--base-url`/`OCP_ADMIN_BASE_URL` override it. The issuer remains the OAuth
token issuer and does not need changing just to use an SSH tunnel for the UI.

The hosted portal connection still supports **pairing and heartbeats only**.
Live portal resource retrieval, remote commands, and portal/connector grant
synchronization are not implemented. Your private-network HTTP portal cannot be
paired remotely until HTTPS and certificate trust are configured. Direct partner
sharing and outbound partner browsing are independent of the hosted portal.

Run the UI integration tests with `python -m pytest tests/test_admin.py`. They
cover authentication, CSRF/Host/Origin checks, throttling, password reset,
identity preservation, source/mapping discovery and previews, API activation,
direct sharing/revocation, portal pairing/heartbeat, and outbound OAuth browsing.

## Requirements

- Python 3.14 or newer
- An Observium Subscription Edition installation with its REST API enabled
- A dedicated Observium API token configured for read-only access
- HTTPS for both partner-to-connector and connector-to-Observium traffic

Observium's documented REST API supports devices, ports, and alerts using an API token.
Its separately documented Graph API returns rendered graph images and uses HTTP Basic
authentication. Because that is not a numeric time-series API, this connector currently
advertises no numeric metric capability and returns a capability-not-supported error
from `/v1/metrics`. It does not fabricate points or store a broader Observium password.

## Development setup

Create a Python 3.14 virtual environment and install the package:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The [`app.env.example`](./app.env.example) file shows the required and integration-specific
environment settings; omitted optional settings use application defaults. Copy it to a
private environment file, replace the secret placeholders, and restrict its permissions
(for example, `chmod 600 app.env`). Do not commit the live environment file. When running
the API as a systemd service, point `EnvironmentFile` to that private file. The example
uses an HTTPS issuer URL; set `OCP_ISSUER_URL` to the connector's actual HTTPS URL before
using it outside localhost. A full connector deployment requires both a configured
source database profile and an Observium URL with a read-only API token.

For a local development instance, use a secret manager rather than shell environment
variables in production:

```powershell
$env:OCP_TOKEN_SIGNING_KEY = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:OCP_ISSUER_URL = "https://connector.company.example"
$env:OCP_TOKEN_AUDIENCE = "open-carrier-portal"
$env:OCP_DATABASE_PATH = ".\ocp.db"
$env:OCP_OBSERVIUM_BASE_URL = "https://observium.company.example"
$env:OCP_OBSERVIUM_API_TOKEN = "<read-only-observium-api-token>"
```

The operational source database connection is separate from the connector metadata
database. Configure a source profile once; it stores connection settings and a mapping
in the connector's SQLite metadata database, but stores only the name of the environment
variable containing the password. Currently supported drivers are MySQL and read-only
SQLite (useful for development or a local NetTool export):

```powershell
$env:OCP_SOURCE_DB_PASSWORD = "<read-only-database-password>"
ocp configure-source `
  --name nettool `
  --driver mysql `
  --host "nettool-db.company.example" `
  --port 3306 `
  --database "nettool" `
  --username "ocp_reader" `
  --password-env OCP_SOURCE_DB_PASSWORD `
  --tls-ca "C:\certs\database-ca.pem"
ocp test-source --name nettool
$env:OCP_SOURCE_DB_PROFILE = "nettool"
```

New `configure-source` profiles also start unmapped. Use the administration mapper or
pass `--mapping-file` explicitly; updating a connection without that option preserves
its existing mapping. `ocp test-source` reports `ocp_contract_ready=false` and missing
fields until the core mapping is configured. Map the fields before using the profile
for partner data.

The optional source example template follows the supplied NPDB schema: `company`, `Location`,
`CompanyLocation`, `RackUnit`, `Device`, `Port`, `LitCircuit`, `Service`, `CircuitLayout`,
`CircuitLayoutItem`, `Patch` and `PseudoWire`. It maps `DeviceName`, `PortName`,
the separate physical-address columns, and `CompanyLocationA`/`CompanyLocationZ`;
it does not invent `Location.Name`, `Location.Address`, or `ALocationID`/`ZLocationID`.
It is verified against an independently declared SQLite fixture shaped like the
supplied schema, not against a live customer MySQL deployment.
`CircuitLayoutItem.TableName` is the
polymorphic item discriminator used by NetTool (`Port`, `Patch`, or `PW`), and `TablePK`
identifies the corresponding source row. Check the mapping against the actual database
with `ocp test-source`; if table or column names differ, provide a JSON mapping file with
`--mapping-file`. Mappings select only explicitly listed columns and accept simple
table/column identifiers, not arbitrary SQL. Use a dedicated read-only database account.
Remote MySQL connections require TLS with a CA file. Keep the password environment
variable available to the connector process as well as to the CLI when testing the
profile. For a trusted, isolated network where the database cannot negotiate modern
verified TLS, an administrator may instead explicitly select plaintext mode:

```bash
ocp configure-source \
  --name nettool \
  --driver mysql \
  --host "10.0.0.20" \
  --port 3306 \
  --database "nettool" \
  --username "ocp_reader" \
  --password-env OCP_SOURCE_DB_PASSWORD \
  --allow-insecure
```

Use either `--tls-ca` for verified TLS or `--allow-insecure` to disable MySQL TLS;
they cannot be combined. Remote connections fail closed if neither option is provided.
Plaintext mode exposes the database credentials and traffic to anyone able to monitor
that network segment. Use it only on a controlled, isolated network with firewall rules
limiting access to the connector host. Prefer verified TLS or an encrypted tunnel.

To authorize a partner for circuits, create its client with the circuit scope and
explicitly share each permitted circuit. `share-circuit` prints the opaque public circuit
ID the partner uses; revoke a share at any time:

```powershell
ocp add-partner --name "Trusted Partner" --scope circuits:read
ocp share-circuit `
  --partner-id "<partner-id>" `
  --circuit-source-id "926" `
  --visibility sfn_path
ocp revoke-circuit-share `
  --partner-id "<partner-id>" `
  --circuit-source-id "926"
```

`endpoints` visibility returns only the configured A/Z ports. `sfn_path` and
`full_shared_path` return the ordered `CircuitLayoutItem` path, resolving only its
`Port`, `Patch`, and `PW` source records. Every returned component ID is opaque and is
available only inside the authorized parent circuit response.

Initialize the owning organization and create the first mutually approved partner:

```powershell
ocp init --organization-name "Example Carrier"
ocp add-partner --name "Trusted Partner"
```

`ocp add-partner` prints the new client secret once. Transfer it through an approved
secret-sharing channel. The bootstrap command currently records both relationship
approvals, so it should be run only after approval has occurred outside the connector.

Run the API:

```powershell
uvicorn open_carrier_portal.main:create_app_from_env --factory --host 127.0.0.1 --port 8000
```

Terminate TLS at an approved reverse proxy or configure an HTTPS-capable application
server before exposing the connector to partners. The interactive OpenAPI documentation
is available at `/docs`.

Run validation:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

## ZIP download and Git

A GitHub ZIP contains project files but not the `.git` directory. This downloaded folder
is therefore not currently a Git repository: it has no commit history, branches, remote,
or normal pull/push behavior. Clone the original repository to retain its history, or run
`git init` here and add the correct GitHub remote before using Git workflows. The files
in this ZIP do not identify the original remote URL.
