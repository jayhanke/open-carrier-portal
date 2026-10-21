from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from open_carrier_portal.errors import AuthenticationError

DEFAULT_SCOPES = {
    "observium:devices:read",
    "observium:interfaces:read",
    "observium:alarms:read",
}
ALLOWED_SCOPES = DEFAULT_SCOPES | {"circuits:read"}


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def generate_client_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_client_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
    )
    return f"scrypt${n}${r}${p}${_b64encode(salt)}${_b64encode(digest)}"


def verify_client_secret(secret: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
    except ValueError, TypeError:
        return False
    return hmac.compare_digest(digest, _b64decode(expected))


@dataclass(frozen=True, slots=True)
class TokenClaims:
    client_id: str
    partner_id: str
    scopes: frozenset[str]
    expires_at: int
    token_id: str


class TokenCodec:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        signing_key: str,
        key_id: str,
        ttl_seconds: int,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._key = signing_key.encode("utf-8")
        self._key_id = key_id
        self._ttl_seconds = ttl_seconds

    def issue(self, client_id: str, partner_id: str, scopes: set[str]) -> tuple[str, int]:
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT", "kid": self._key_id}
        payload = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": client_id,
            "partner_id": partner_id,
            "scope": " ".join(sorted(scopes)),
            "iat": now,
            "exp": now + self._ttl_seconds,
            "jti": secrets.token_urlsafe(16),
        }
        encoded_header = _b64encode(
            json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        encoded_payload = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = hmac.new(self._key, signing_input, hashlib.sha256).digest()
        return f"{encoded_header}.{encoded_payload}.{_b64encode(signature)}", self._ttl_seconds

    def decode(self, token: str) -> TokenClaims:
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
            expected = hmac.new(self._key, signing_input, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64decode(encoded_signature)):
                raise AuthenticationError("Invalid access token")
            header = self._decode_json(encoded_header)
            payload = self._decode_json(encoded_payload)
            self._validate_header(header)
            return self._validate_payload(payload)
        except AuthenticationError:
            raise
        except ValueError, TypeError, KeyError, json.JSONDecodeError:
            raise AuthenticationError("Invalid access token") from None

    @staticmethod
    def _decode_json(encoded: str) -> dict[str, Any]:
        value = json.loads(_b64decode(encoded))
        if not isinstance(value, dict):
            raise ValueError("JWT segment must be an object")
        return value

    def _validate_header(self, header: dict[str, Any]) -> None:
        if (
            header.get("alg") != "HS256"
            or header.get("typ") != "JWT"
            or header.get("kid") != self._key_id
        ):
            raise AuthenticationError("Invalid access token")

    def _validate_payload(self, payload: dict[str, Any]) -> TokenClaims:
        now = int(time.time())
        if payload.get("iss") != self._issuer or payload.get("aud") != self._audience:
            raise AuthenticationError("Invalid access token")
        expires_at = payload.get("exp")
        issued_at = payload.get("iat")
        if (
            not isinstance(expires_at, int)
            or not isinstance(issued_at, int)
            or expires_at <= now
            or issued_at > now + 60
        ):
            raise AuthenticationError("Expired or invalid access token")
        client_id = payload.get("sub")
        partner_id = payload.get("partner_id")
        token_id = payload.get("jti")
        scope = payload.get("scope", "")
        if not all(isinstance(value, str) and value for value in (client_id, partner_id, token_id)):
            raise AuthenticationError("Invalid access token")
        if not isinstance(scope, str):
            raise AuthenticationError("Invalid access token")
        return TokenClaims(
            client_id=client_id,
            partner_id=partner_id,
            scopes=frozenset(scope.split()),
            expires_at=expires_at,
            token_id=token_id,
        )


class CursorCodec:
    def __init__(self, signing_key: str) -> None:
        self._key = signing_key.encode("utf-8")

    def encode(self, *, partner_id: str, route: str, page: int) -> str:
        payload = json.dumps(
            {"partner_id": partner_id, "route": route, "page": page},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def decode(self, cursor: str, *, partner_id: str, route: str) -> int:
        try:
            encoded_payload, encoded_signature = cursor.split(".")
            payload_bytes = _b64decode(encoded_payload)
            expected = hmac.new(self._key, payload_bytes, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64decode(encoded_signature)):
                raise ValueError
            payload = json.loads(payload_bytes)
            if (
                payload.get("partner_id") != partner_id
                or payload.get("route") != route
                or not isinstance(payload.get("page"), int)
                or payload["page"] < 1
            ):
                raise ValueError
            return payload["page"]
        except ValueError, TypeError, KeyError, json.JSONDecodeError:
            from open_carrier_portal.errors import ConnectorError

            raise ConnectorError(
                400,
                "invalid_cursor",
                "The pagination cursor is invalid",
            ) from None
