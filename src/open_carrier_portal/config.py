from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from open_carrier_portal.errors import ConfigurationError

PRIVATE_NETWORKS = tuple(
    ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def is_private_network_host(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def validate_connector_url(value: str, *, allow_insecure_http: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("invalid connector URL") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port == 0
    ):
        raise ConfigurationError("connector URL must be an origin without credentials or a path")
    if parsed.scheme == "https":
        return value.rstrip("/")
    if parsed.scheme == "http" and (
        parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        or (allow_insecure_http and is_private_network_host(parsed.hostname))
    ):
        return value.rstrip("/")
    raise ConfigurationError(
        "connector URL must use HTTPS outside localhost; trusted private-IP HTTP testing "
        "requires OCP_ALLOW_INSECURE_HTTP=true"
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _env_int(name: str, default: int, minimum: int) -> int:
    value = os.getenv(name)
    parsed = default if value is None else int(value)
    if parsed < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    issuer_url: str
    token_audience: str
    token_signing_key: str
    token_key_id: str = "default"
    access_token_ttl_seconds: int = 900
    max_page_size: int = 100
    max_metric_points: int = 10_000
    max_metric_range_seconds: int = 604_800
    observium_base_url: str | None = None
    observium_api_token: str | None = None
    observium_verify_tls: bool = True
    observium_ca_bundle: str | None = None
    observium_timeout_seconds: int = 15
    source_database_profile: str | None = None
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        if len(self.token_signing_key.encode("utf-8")) < 32:
            raise ConfigurationError("OCP_TOKEN_SIGNING_KEY must contain at least 32 bytes")
        validate_connector_url(self.issuer_url, allow_insecure_http=self.allow_insecure_http)
        if self.observium_base_url:
            if not self.observium_base_url.startswith("https://"):
                raise ConfigurationError("OCP_OBSERVIUM_BASE_URL must use HTTPS")
            if not self.observium_api_token:
                raise ConfigurationError(
                    "OCP_OBSERVIUM_API_TOKEN is required when Observium is enabled"
                )
            if not self.observium_verify_tls:
                raise ConfigurationError("Observium TLS verification cannot be disabled")

    @property
    def observium_enabled(self) -> bool:
        return bool(self.observium_base_url and self.observium_api_token)

    @classmethod
    def from_env(cls) -> Settings:
        signing_key = os.getenv("OCP_TOKEN_SIGNING_KEY")
        if not signing_key:
            raise ConfigurationError("OCP_TOKEN_SIGNING_KEY is required")
        return cls(
            database_path=Path(os.getenv("OCP_DATABASE_PATH", "ocp.db")),
            issuer_url=os.getenv("OCP_ISSUER_URL", "http://localhost:8000").rstrip("/"),
            token_audience=os.getenv("OCP_TOKEN_AUDIENCE", "open-carrier-portal"),
            token_signing_key=signing_key,
            token_key_id=os.getenv("OCP_TOKEN_KEY_ID", "default"),
            access_token_ttl_seconds=_env_int("OCP_ACCESS_TOKEN_TTL_SECONDS", 900, 60),
            max_page_size=_env_int("OCP_MAX_PAGE_SIZE", 100, 1),
            max_metric_points=_env_int("OCP_MAX_METRIC_POINTS", 10_000, 1),
            max_metric_range_seconds=_env_int("OCP_MAX_METRIC_RANGE_SECONDS", 604_800, 60),
            observium_base_url=os.getenv("OCP_OBSERVIUM_BASE_URL"),
            observium_api_token=os.getenv("OCP_OBSERVIUM_API_TOKEN"),
            observium_verify_tls=_env_bool("OCP_OBSERVIUM_VERIFY_TLS", True),
            observium_ca_bundle=os.getenv("OCP_OBSERVIUM_CA_BUNDLE"),
            observium_timeout_seconds=_env_int("OCP_OBSERVIUM_TIMEOUT_SECONDS", 15, 1),
            source_database_profile=os.getenv("OCP_SOURCE_DB_PROFILE"),
            allow_insecure_http=_env_bool("OCP_ALLOW_INSECURE_HTTP", False),
        )
