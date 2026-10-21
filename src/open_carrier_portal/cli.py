from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from getpass import getpass
from pathlib import Path

import httpx

from open_carrier_portal.config import Settings
from open_carrier_portal.models import ConnectorIdentity
from open_carrier_portal.portal_agent import (
    DEFAULT_CONNECTION_FILE,
    PortalConnection,
    pair_portal,
    run_agent,
)
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import (
    ALLOWED_SCOPES,
    DEFAULT_SCOPES,
    generate_client_secret,
    hash_client_secret,
)
from open_carrier_portal.source_database import SourceMapping, configured_source_database


def main() -> None:
    parser = argparse.ArgumentParser(prog="ocp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize the owning organization")
    init_parser.add_argument("--organization-name", required=True)
    init_parser.add_argument("--organization-id", default=None)

    partner_parser = subparsers.add_parser(
        "add-partner",
        help="Create an active bilateral partner, grant, and OAuth client",
    )
    partner_parser.add_argument("--name", required=True)
    partner_parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        choices=sorted(ALLOWED_SCOPES),
    )

    share_circuit_parser = subparsers.add_parser(
        "share-circuit",
        help="Share a NetTool circuit with a partner",
    )
    share_circuit_parser.add_argument("--partner-id", required=True)
    share_circuit_parser.add_argument("--circuit-source-id", required=True)
    share_circuit_parser.add_argument(
        "--visibility",
        choices=["summary", "endpoints", "sfn_path", "full_shared_path", "metrics"],
        required=True,
    )

    revoke_circuit_parser = subparsers.add_parser(
        "revoke-circuit-share",
        help="Revoke a partner's NetTool circuit share",
    )
    revoke_circuit_parser.add_argument("--partner-id", required=True)
    revoke_circuit_parser.add_argument("--circuit-source-id", required=True)

    source_parser = subparsers.add_parser(
        "configure-source",
        help="Create or update a NetTool source database profile",
    )
    source_parser.add_argument("--name", default="nettool")
    source_parser.add_argument("--driver", choices=["mysql", "sqlite"], default="mysql")
    source_parser.add_argument("--host")
    source_parser.add_argument("--port", type=int, default=3306)
    source_parser.add_argument("--database", required=True)
    source_parser.add_argument("--username")
    source_parser.add_argument("--password-env", default="OCP_SOURCE_DB_PASSWORD")
    source_parser.add_argument("--tls-ca")
    source_parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help="Allow unencrypted remote MySQL traffic on a trusted isolated network",
    )
    source_parser.add_argument("--timeout-seconds", type=int, default=10)
    source_parser.add_argument("--mapping-file", type=Path)
    source_parser.add_argument("--disabled", action="store_true")

    test_source_parser = subparsers.add_parser(
        "test-source",
        help="Test a configured source database profile and its mappings",
    )
    test_source_parser.add_argument("--name", default="nettool")

    pair_parser = subparsers.add_parser(
        "pair-portal", help="Pair with a hosted portal using a code"
    )
    pair_parser.add_argument("--portal-url", required=True)
    pair_parser.add_argument("--connection-file", type=Path, default=DEFAULT_CONNECTION_FILE)
    agent_parser = subparsers.add_parser(
        "run-portal-agent", help="Maintain an outbound connection to the hosted portal"
    )
    agent_parser.add_argument("--connection-file", type=Path, default=DEFAULT_CONNECTION_FILE)
    agent_parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")

    for command in ("create-admin", "reset-admin-password"):
        admin_parser = subparsers.add_parser(command, help="Manage a local connector administrator")
        admin_parser.add_argument("--username", required=True)
    for command in ("serve", "serve-admin"):
        serve_parser = subparsers.add_parser(
            command, help="Run the connector API and administration UI together"
        )
        serve_parser.add_argument("--host", default="127.0.0.1")
        serve_parser.add_argument("--port", type=int, default=8000)
        serve_parser.add_argument("--base-url", default=os.getenv("OCP_ADMIN_BASE_URL"))
        serve_parser.add_argument(
            "--connection-file",
            type=Path,
            default=Path(os.getenv("OCP_PORTAL_CONNECTION_FILE", str(DEFAULT_CONNECTION_FILE))),
        )

    args = parser.parse_args()
    settings = Settings.from_env()
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()

    if args.command in {"create-admin", "reset-admin-password", "serve", "serve-admin"}:
        from open_carrier_portal.admin_store import AdminStore

        store = AdminStore(settings.database_path)
        store.initialize()
        if args.command in {"serve", "serve-admin"}:
            import uvicorn

            from open_carrier_portal.main import create_app

            if args.host not in {"127.0.0.1", "::1", "localhost"}:
                parser.error(
                    "connector listener must be loopback; "
                    "use an SSH tunnel or trusted HTTPS reverse proxy"
                )
            uvicorn.run(
                create_app(
                    settings,
                    admin_base_url=args.base_url or f"http://127.0.0.1:{args.port}",
                    connection_file=args.connection_file,
                ),
                host=args.host,
                port=args.port,
            )
            return
        password = getpass("Local administrator password (10-256 characters): ")
        if password != getpass("Confirm password: "):
            parser.error("passwords do not match")
        try:
            store.set_password(
                args.username, password, reset=args.command == "reset-admin-password"
            )
        except ValueError as exc:
            parser.error(str(exc))
        print("Local administrator saved. Existing admin sessions for this account were revoked.")
        return

    if args.command in {"pair-portal", "run-portal-agent"}:
        identity = repository.get_connector_identity()
        if identity is None or not identity.enabled:
            parser.error("initialize an enabled connector first with ocp init")
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        # Never log HTTP requests or credentials, including through httpx's info logger.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        try:
            with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
                if args.command == "pair-portal":
                    pair_portal(
                        client=client,
                        portal_url=args.portal_url,
                        connector_id=identity.connector_id,
                        code=getpass("One-time portal pairing code: ").strip(),
                        connection_file=args.connection_file,
                    )
                    print(
                        f"Paired. Private credentials saved to {args.connection_file}. "
                        "Run ocp run-portal-agent to connect."
                    )
                else:
                    connection = PortalConnection.load(args.connection_file)
                    if connection.connector_id != identity.connector_id:
                        parser.error("connection belongs to a different connector; pair again")
                    run_agent(client, connection, once=args.once)
        except FileExistsError:
            parser.error(
                "connection file already exists; choose a new --connection-file to replace pairing"
            )
        except httpx.HTTPStatusError as exc:
            parser.error(
                f"portal returned HTTP {exc.response.status_code}; "
                "check the portal URL and pairing/connection status"
            )
        except httpx.RequestError:
            parser.error("portal is unreachable; check the URL, TLS trust, and network")
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        except KeyboardInterrupt:
            print("Portal agent stopped.")
        return

    if args.command == "init":
        identity = ConnectorIdentity(
            organization_id=args.organization_id or str(uuid.uuid4()),
            organization_name=args.organization_name,
            connector_id=str(uuid.uuid4()),
            issuer_url=settings.issuer_url,
        )
        repository.put_connector_identity(identity)
        print(identity.model_dump_json(indent=2))
        return

    if args.command == "add-partner":
        scopes = set(args.scopes or DEFAULT_SCOPES)
        client_secret = generate_client_secret()
        partner_id, relationship_id, client_id = repository.provision_partner(
            name=args.name,
            secret_hash=hash_client_secret(client_secret),
            grants={scope: set() for scope in scopes},
        )
        print(
            json.dumps(
                {
                    "partner_id": partner_id,
                    "relationship_id": relationship_id,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scopes": sorted(scopes),
                    "warning": "Store the client_secret now; it will not be shown again.",
                },
                indent=2,
            )
        )
        return

    if args.command == "share-circuit":
        public_circuit_id = repository.share_circuit(
            partner_id=args.partner_id,
            source_circuit_id=args.circuit_source_id,
            visibility=args.visibility,
        )
        print(
            json.dumps(
                {
                    "partner_id": args.partner_id,
                    "circuit_id": public_circuit_id,
                    "visibility": args.visibility,
                    "status": "active",
                },
                indent=2,
            )
        )
        return

    if args.command == "revoke-circuit-share":
        revoked = repository.revoke_circuit_share(
            partner_id=args.partner_id,
            source_circuit_id=args.circuit_source_id,
        )
        print(
            json.dumps(
                {
                    "partner_id": args.partner_id,
                    "status": "revoked" if revoked else "not_found_or_already_revoked",
                },
                indent=2,
            )
        )
        return

    if args.command == "configure-source":
        if args.driver == "mysql" and (not args.host or not args.username):
            parser.error("mysql requires --host and --username")
        if args.driver == "mysql" and args.tls_ca and args.allow_insecure:
            parser.error("--tls-ca and --allow-insecure cannot be used together")
        if (
            args.driver == "mysql"
            and not args.tls_ca
            and not args.allow_insecure
            and args.host.lower() not in {"localhost", "127.0.0.1", "::1"}
        ):
            parser.error("remote MySQL requires --tls-ca or explicit --allow-insecure opt-in")
        if args.driver != "mysql" and args.allow_insecure:
            parser.error("--allow-insecure applies only to remote MySQL profiles")
        mapping = (
            SourceMapping.nettool_defaults()
            if args.mapping_file is None
            else SourceMapping.from_json(args.mapping_file.read_text(encoding="utf-8"))
        )
        profile_id = repository.put_source_database_profile(
            name=args.name,
            driver=args.driver,
            host=args.host if args.driver == "mysql" else None,
            port=args.port if args.driver == "mysql" else None,
            database_name=args.database,
            username=args.username if args.driver == "mysql" else None,
            password_env_var=args.password_env if args.driver == "mysql" else None,
            tls_ca_path=args.tls_ca if args.driver == "mysql" else None,
            allow_insecure=args.allow_insecure,
            timeout_seconds=args.timeout_seconds,
            mapping_json=mapping.to_json(),
            enabled=not args.disabled,
        )
        print(
            json.dumps(
                {
                    "profile_id": profile_id,
                    "name": args.name,
                    "driver": args.driver,
                    "database": args.database,
                    "password_env": (args.password_env if args.driver == "mysql" else None),
                    "allow_insecure": args.allow_insecure,
                    "enabled": not args.disabled,
                },
                indent=2,
            )
        )
        return

    profile = repository.get_source_database_profile(args.name)
    if profile is None:
        parser.error(f"source database profile {args.name!r} does not exist")
    if not profile.enabled:
        parser.error(f"source database profile {args.name!r} is disabled")
    source = configured_source_database(
        driver=profile.driver,
        host=profile.host,
        port=profile.port,
        database_name=profile.database_name,
        username=profile.username,
        password_env_var=profile.password_env_var,
        tls_ca_path=profile.tls_ca_path,
        allow_insecure=profile.allow_insecure,
        timeout_seconds=profile.timeout_seconds,
        mapping_json=profile.mapping_json,
        environment=os.environ,
    )
    source.health_check()
    source.validate_mapping()
    print(
        json.dumps(
            {
                "name": profile.name,
                "driver": profile.driver,
                "status": "ok",
                "mapped_resources": sorted(SourceMapping.from_json(profile.mapping_json).resources),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
