from pathlib import Path

import pytest

from open_carrier_portal.config import Settings, validate_connector_url
from open_carrier_portal.errors import ConfigurationError


@pytest.mark.parametrize("host", ["10.210.4.223", "172.16.0.1", "192.168.1.1", "[fd00::1]"])
def test_private_http_requires_explicit_opt_in(host):
    url = f"http://{host}:8000"
    with pytest.raises(ConfigurationError, match="OCP_ALLOW_INSECURE_HTTP"):
        validate_connector_url(url)
    assert validate_connector_url(url, allow_insecure_http=True) == url
    with pytest.raises(ConfigurationError):
        Settings(Path("metadata.db"), url, "test", "s" * 48)
    settings = Settings(Path("metadata.db"), url, "test", "s" * 48, allow_insecure_http=True)
    assert settings.issuer_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:8000",
        "http://169.254.1.1:8000",
        "http://100.64.0.1:8000",
        "http://0.0.0.0:8000",
        "http://[::]:8000",
        "http://[fe80::1]:8000",
        "http://localhost.attacker.test",
        "http://connector.internal:8000",
        "http://user:password@10.210.4.223:8000",
        "http://10.210.4.223:8000/admin",
        "http://10.210.4.223:8000?query=1",
        "http://10.210.4.223:8000#fragment",
        "http://10.210.4.223:invalid",
        "http://10.210.4.223:0",
    ],
)
def test_http_opt_in_does_not_allow_public_or_invalid_origins(url):
    with pytest.raises(ConfigurationError):
        validate_connector_url(url, allow_insecure_http=True)


def test_http_opt_in_from_environment(monkeypatch):
    monkeypatch.setenv("OCP_TOKEN_SIGNING_KEY", "s" * 48)
    monkeypatch.setenv("OCP_ISSUER_URL", "http://10.210.4.223:8000")
    monkeypatch.delenv("OCP_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ConfigurationError):
        Settings.from_env()
    monkeypatch.setenv("OCP_ALLOW_INSECURE_HTTP", "true")
    assert Settings.from_env().allow_insecure_http
    monkeypatch.setenv("OCP_ALLOW_INSECURE_HTTP", "invalid")
    with pytest.raises(ConfigurationError, match="must be true or false"):
        Settings.from_env()
