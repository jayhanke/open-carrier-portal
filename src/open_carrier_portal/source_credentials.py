from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from open_carrier_portal.errors import ConfigurationError


def credential_path(database_path: Path, variable: str) -> Path:
    directory = database_path.resolve().with_name(database_path.name + ".credentials")
    return directory / (hashlib.sha256(variable.encode()).hexdigest() + ".secret")


def read_password(database_path: Path, variable: str) -> str | None:
    path = credential_path(database_path, variable)
    if not path.exists():
        return None
    if os.name != "nt" and (path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077):
        raise ConfigurationError("database credential file and directory must be private")
    return path.read_text(encoding="utf-8")


def save_password(database_path: Path, variable: str, password: str) -> None:
    path = credential_path(database_path, variable)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and path.parent.stat().st_mode & 0o077:
        raise ConfigurationError("database credential directory must be private (chmod 700)")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(password)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
