from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from open_carrier_portal.errors import AuthenticationError, AuthorizationError, RateLimitError
from open_carrier_portal.repository import Repository
from open_carrier_portal.security import TokenClaims, TokenCodec, verify_client_secret


@dataclass(frozen=True, slots=True)
class Principal:
    client_id: str
    partner_id: str
    scopes: frozenset[str]
    token_id: str


class AuthenticationService:
    def __init__(self, repository: Repository, token_codec: TokenCodec) -> None:
        self._repository = repository
        self._token_codec = token_codec
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._failure_lock = threading.Lock()

    def issue_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        requested_scopes: set[str],
    ) -> tuple[str, int, set[str]]:
        self._check_throttle(client_id)
        try:
            client = self._repository.get_active_client(client_id)
        except AuthenticationError:
            self._record_failure(client_id)
            raise
        if not verify_client_secret(client_secret, client.secret_hash):
            self._record_failure(client_id)
            raise AuthenticationError()
        if requested_scopes and not requested_scopes.issubset(client.scopes):
            raise AuthorizationError("One or more requested scopes are not allowed")
        granted_scopes = requested_scopes or set(client.scopes)
        self._clear_failures(client_id)
        token, expires_in = self._token_codec.issue(
            client.client_id,
            client.partner_id,
            granted_scopes,
        )
        return token, expires_in, granted_scopes

    def authenticate(self, token: str) -> Principal:
        claims: TokenClaims = self._token_codec.decode(token)
        client = self._repository.get_active_client(claims.client_id)
        if client.partner_id != claims.partner_id or not claims.scopes.issubset(client.scopes):
            raise AuthenticationError("Access token is no longer valid")
        return Principal(
            client_id=claims.client_id,
            partner_id=claims.partner_id,
            scopes=claims.scopes,
            token_id=claims.token_id,
        )

    def _check_throttle(self, client_id: str) -> None:
        now = time.monotonic()
        with self._failure_lock:
            failures = self._failures[client_id]
            while failures and failures[0] < now - 300:
                failures.popleft()
            if len(failures) >= 5:
                retry_after = max(1, int(300 - (now - failures[0])))
                raise RateLimitError(retry_after)

    def _record_failure(self, client_id: str) -> None:
        with self._failure_lock:
            self._failures[client_id].append(time.monotonic())

    def _clear_failures(self, client_id: str) -> None:
        with self._failure_lock:
            self._failures.pop(client_id, None)
