"""Run Alembic migrations without an alembic.ini.

Usage: python -m sentinel_storage.migrate [upgrade|downgrade|check|current] [revision]
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from sentinel_storage.db import DatabaseSettings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url or DatabaseSettings().url)
    return cfg


def main(argv: list[str]) -> int:
    action = argv[0] if argv else "upgrade"
    cfg = alembic_config()
    if action == "upgrade":
        command.upgrade(cfg, argv[1] if len(argv) > 1 else "head")
    elif action == "downgrade":
        command.downgrade(cfg, argv[1] if len(argv) > 1 else "-1")
    elif action == "check":
        command.check(cfg)
    elif action == "current":
        command.current(cfg, verbose=True)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
