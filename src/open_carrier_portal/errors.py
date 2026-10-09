from __future__ import annotations

from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Raised when connector configuration is unsafe or incomplete."""


class SourceDatabaseError(RuntimeError):
    """Base error for operational source database failures."""


class SourceDatabaseUnavailableError(SourceDatabaseError):
    """Raised when the configured source database cannot be reached."""


class SourceDatabaseQueryError(SourceDatabaseError):
    """Raised when an approved source query or mapping cannot be executed."""


@dataclass(slots=True)
class ConnectorError(Exception):
    status_code: int
    code: str
    message: str
    retry_after_seconds: int | None = None


class AuthenticationError(ConnectorError):
    def __init__(self, message: str = "Invalid client credentials") -> None:
        super().__init__(401, "authentication_failed", message)


class AuthorizationError(ConnectorError):
    def __init__(self, message: str = "Access is not permitted") -> None:
        super().__init__(403, "access_denied", message)


class ResourceNotFoundError(ConnectorError):
    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(404, "resource_not_found", message)


class CapabilityNotSupportedError(ConnectorError):
    def __init__(self, message: str) -> None:
        super().__init__(501, "capability_not_supported", message)


class IntegrationUnavailableError(ConnectorError):
    def __init__(self, message: str = "Observium integration is unavailable") -> None:
        super().__init__(503, "integration_unavailable", message)


class UpstreamResponseError(ConnectorError):
    def __init__(self, message: str = "Observium returned an invalid response") -> None:
        super().__init__(502, "invalid_upstream_response", message)


class UpstreamTimeoutError(ConnectorError):
    def __init__(self) -> None:
        super().__init__(504, "upstream_timeout", "Observium did not respond before the deadline")


class RateLimitError(ConnectorError):
    def __init__(self, retry_after_seconds: int | None = None) -> None:
        super().__init__(
            429,
            "rate_limited",
            "The request rate limit was exceeded",
            retry_after_seconds,
        )
