"""Secret loading: prefer mounted files (``*_FILE``) over plain environment variables."""

from __future__ import annotations

import os
from pathlib import Path


class MissingSecretError(RuntimeError):
    pass


def read_secret(name: str, *, required: bool = True, min_length: int = 16) -> str | None:
    """Read ``$NAME_FILE`` if set, else ``$NAME``. Enforces a minimum length."""
    file_path = os.environ.get(f"{name}_FILE")
    value: str | None
    value = Path(file_path).read_text().strip() if file_path else os.environ.get(name)
    if not value:
        if required:
            raise MissingSecretError(f"secret {name} is not configured ({name}_FILE or {name})")
        return None
    if len(value) < min_length:
        raise MissingSecretError(f"secret {name} is shorter than {min_length} characters")
    return value
