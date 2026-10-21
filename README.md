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
- `GET /v1/circuits/{circuit_id}/path`
- `GET /health/live` and `GET /health/ready`

Resource IDs are opaque, list results are filtered by the partner's grant, pagination
cursors are signed and partner-bound, and access decisions are audited in the local
SQLite metadata database. Operational network data remains in Observium and NetTool.
Circuit path access additionally requires an active `circuits:read` grant and an
explicit active share for that partner; a circuit share alone never exposes other
circuit records or independent path-component routes.

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

- **Setup:** readiness checklist and explicit source connectivity/mapping check.
  Portal pairing is optional; a heartbeat does not mean source data is ready.
- **Identity:** initialize or rename your company without rotating identity IDs.
- **Portal:** enter the HTTPS portal URL and one-time company-admin pairing code.
  An optional PEM CA path supports an internal certificate authority. The pairing
  credential is saved locally using the existing protected connection file, never
  rendered back into a page. While the connector runs it sends heartbeats every
  30 seconds. Use **Test heartbeat now** to verify immediately. Do not run another
  `run-portal-agent` against the same connection file simultaneously.
  **Disconnect locally** deletes the saved credential; also revoke the connection
  in the hosted portal to invalidate the server-side credential. Disconnect before
  replacing a pairing. `serve --connection-file PATH` selects an existing
  CLI pairing file or a service-account-specific private location.
- **Sources:** configure/reuse named MySQL or read-only SQLite profiles. Password
  fields accept an **environment variable name**, never the password itself.
  Use absolute paths for SQLite. Remote MySQL requires a trusted TLS CA; existing
  CLI plaintext opt-ins are preserved but cannot be newly enabled through the UI.
  Connectivity/mapping tests are SELECT-only and **cannot prove** a MySQL account
  has no write privileges; provision a dedicated read-only account yourself.
  Save optional Observium URL, token environment-variable name and CA path, then
  test it separately. Secrets remain on this connector, never in the hosted portal.
- **Mapping:** use guided table/column fields, or advanced JSON. Discover up to
  1,000 column identifiers and preview at most five rows of explicitly mapped
  columns. No arbitrary SQL editor is provided. Every enabled mapped resource must
  have a `source_id`. Blank table names remove a mapped resource; blank column
  fields remove logical fields. Invalid mappings are not saved. Validation checks
  tables and columns, not complete source-data referential integrity.
- **Partners:** provision direct inbound partners after confirming bilateral
  approval out of band. Select scopes and specific opaque resource IDs, or
  explicitly opt in to all resources for those scopes. Client secrets appear
  **once**, only in the successful creation response. Copy them privately;
  diagnostics and subsequent pages do not reveal stored secrets/hashes.
  Circuit shares additionally require a matching `circuits:read` grant. Revoking
  a partner disables its relationship, OAuth clients and circuit shares; already
  issued tokens then fail the API's existing live authorization checks.
- **Remote:** configure named outbound connections to other companies' connectors,
  using OAuth credentials they issue to you. Each secret stays in a named local
  environment variable. Test token acquisition and capabilities, or retrieve an
  explicitly authorized opaque circuit path. The connector's capabilities endpoint
  requires `observium:devices:read` and an available monitoring adapter; circuits-only
  connections report OAuth authentication separately without requesting capabilities.
  HTTPS is required outside localhost;
  TLS verification cannot be disabled, redirects are rejected, and tokens are
  neither persisted nor displayed. This is manual browsing, not background
  synchronization. Saving a new connection name preserves other saved connections.
- **Diagnostics:** bounded, escaped views of the latest 50 local administration
  and partner API audit events. No raw configuration/secret bundle is exported.
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

Admin sessions expire after one hour, are stored server-side as token hashes, and
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

The default mapping covers NetTool locations, devices, ports, circuits, circuit layouts,
ordered layout items, patches, and pseudowires. `CircuitLayoutItem.TableName` is the
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
