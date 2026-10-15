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
using it outside localhost.

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
