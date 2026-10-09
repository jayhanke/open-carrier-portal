from __future__ import annotations

import pytest

from open_carrier_portal.errors import AuthenticationError
from open_carrier_portal.security import TokenCodec, hash_client_secret, verify_client_secret


def test_client_secret_hash_round_trip() -> None:
    encoded = hash_client_secret("correct horse battery staple")

    assert "correct horse battery staple" not in encoded
    assert verify_client_secret("correct horse battery staple", encoded)
    assert not verify_client_secret("wrong", encoded)


def test_token_rejects_tampering() -> None:
    codec = TokenCodec(
        issuer="https://connector.example",
        audience="ocp",
        signing_key="a" * 32,
        key_id="test",
        ttl_seconds=300,
    )
    token, _ = codec.issue("client", "partner", {"observium:devices:read"})
    header, payload, signature = token.split(".")

    claims = codec.decode(token)
    assert claims.client_id == "client"
    assert claims.scopes == frozenset({"observium:devices:read"})

    with pytest.raises(AuthenticationError):
        codec.decode(f"{header}.{payload}.{signature[:-1]}A")
