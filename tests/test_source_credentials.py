import os
from pathlib import Path

import pytest

from open_carrier_portal.errors import ConfigurationError
from open_carrier_portal.source_credentials import credential_path, read_password, save_password


def test_credentials_private_atomic_and_separate(tmp_path: Path):
    metadata = tmp_path / "metadata.db"
    assert read_password(metadata, "FIRST") is None
    save_password(metadata, "FIRST", " spaces $ and unicode \u00e9 ")
    save_password(metadata, "SECOND", "another-secret")
    assert read_password(metadata, "FIRST") == " spaces $ and unicode \u00e9 "
    save_password(metadata, "FIRST", "replacement")
    assert read_password(metadata, "FIRST") == "replacement"
    assert read_password(metadata, "SECOND") == "another-secret"
    path = credential_path(metadata, "FIRST")
    assert not list(path.parent.glob(".pending-*"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        path.chmod(0o644)
        with pytest.raises(ConfigurationError, match="private"):
            read_password(metadata, "FIRST")
