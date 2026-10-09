from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from open_carrier_portal.config import Settings
from open_carrier_portal.models import ConnectorIdentity
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import generate_client_secret, hash_client_secret
from open_carrier_portal.source_database import SourceMapping, configured_source_database

DEFAULT_SCOPES = {
    "observium:devices:read",
    "observium:interfaces:read",
    "observium:alarms:read",
}
ALLOWED_SCOPES = DEFAULT_SCOPES | {"circuits:read"}


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
    source_parser.add_argument("--timeout-seconds", type=int, default=10)
    source_parser.add_argument("--mapping-file", type=Path)
    source_parser.add_argument("--disabled", action="store_true")

    test_source_parser = subparsers.add_parser(
        "test-source",
        help="Test a configured source database profile and its mappings",
    )
    test_source_parser.add_argument("--name", default="nettool")

    args = parser.parse_args()
    settings = Settings.from_env()
    repository = Repository(settings.database_path, settings.token_signing_key)
    repository.initialize()

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
        partner_id = repository.create_partner(args.name)
        relationship_id = repository.create_active_relationship(partner_id)
        for scope in scopes:
            repository.create_grant(relationship_id, scope)
        client_secret = generate_client_secret()
        client_id = repository.create_client(
            partner_id,
            hash_client_secret(client_secret),
            scopes,
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
