#!/usr/bin/env bash
# Check prerequisites and install the Python toolchain into the repo (.cache/, .venv/).
# Writes nothing outside the repository.
set -euo pipefail
cd "$(dirname "$0")/.."

missing=0
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 — $2"; missing=1; }; }
need uv "https://docs.astral.sh/uv/getting-started/installation/"
need docker "Docker Desktop / OrbStack / Colima"
need curl "system package manager"
[[ $missing -eq 0 ]] || exit 1

docker info >/dev/null 2>&1 || { echo "docker daemon not reachable — start Docker first"; exit 1; }

export UV_CACHE_DIR="$PWD/.cache/uv" UV_PYTHON_INSTALL_DIR="$PWD/.cache/python" \
       UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_INSTALL_BIN=0
uv python install 3.12
uv sync --frozen
[[ -f .env ]] || { cp .env.example .env; echo "created .env from .env.example"; }
echo "bootstrap complete — next: make up"
