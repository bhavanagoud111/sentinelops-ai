"""Demo-only fault injection.

Faults are in-memory toggles exposed on an admin router guarded by a shared
token. This exists only in sandbox demo workloads — never in the SentinelOps
control plane — and the router is not mounted at all when no token is set.
"""

from __future__ import annotations

import hmac
import logging
import random
import threading
from collections.abc import Callable, Iterable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class FaultSpec(BaseModel):
    """Parameters for an active fault."""

    probability: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    delay_ms: Annotated[int, Field(ge=0, le=30_000)] = 0


class FaultRegistry:
    """Thread-safe set of active faults, restricted to a declared allowlist."""

    def __init__(self, allowed: Iterable[str], rng: random.Random | None = None) -> None:
        self._allowed = frozenset(allowed)
        self._active: dict[str, FaultSpec] = {}
        self._lock = threading.Lock()
        self._rng = rng or random.Random()  # noqa: S311 - not security sensitive

    @property
    def allowed(self) -> frozenset[str]:
        return self._allowed

    def activate(self, name: str, spec: FaultSpec) -> None:
        self._check(name)
        with self._lock:
            self._active[name] = spec
        logger.warning("fault activated", extra={"fault": name, **spec.model_dump()})

    def clear(self, name: str) -> None:
        self._check(name)
        with self._lock:
            self._active.pop(name, None)
        logger.warning("fault cleared", extra={"fault": name})

    def clear_all(self) -> None:
        with self._lock:
            self._active.clear()
        logger.warning("all faults cleared")

    def get(self, name: str) -> FaultSpec | None:
        with self._lock:
            return self._active.get(name)

    def snapshot(self) -> dict[str, FaultSpec]:
        with self._lock:
            return dict(self._active)

    def triggered(self, name: str) -> FaultSpec | None:
        """Return the spec if the fault is active *and* fires for this request."""
        spec = self.get(name)
        if spec is None:
            return None
        return spec if self._rng.random() < spec.probability else None

    def _check(self, name: str) -> None:
        if name not in self._allowed:
            raise KeyError(name)


def admin_router(registry: FaultRegistry, token: str) -> APIRouter:
    """Build the ``/admin/faults`` router. ``token`` must be non-empty."""
    if not token:
        raise ValueError("fault admin token must be non-empty")

    def require_token(x_fault_token: Annotated[str | None, Header()] = None) -> None:
        if x_fault_token is None or not hmac.compare_digest(x_fault_token, token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid fault token")

    router = APIRouter(prefix="/admin/faults", tags=["demo-faults"])
    guard: list[Any] = [Depends(require_token)]

    def _known(name: str) -> None:
        if name not in registry.allowed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown fault {name!r}")

    @router.get("", dependencies=guard)
    def list_faults() -> dict[str, Any]:
        return {
            "allowed": sorted(registry.allowed),
            "active": {k: v.model_dump() for k, v in registry.snapshot().items()},
        }

    @router.put("/{name}", dependencies=guard, status_code=status.HTTP_204_NO_CONTENT)
    def activate(name: str, spec: FaultSpec) -> None:
        _known(name)
        registry.activate(name, spec)

    @router.delete("/{name}", dependencies=guard, status_code=status.HTTP_204_NO_CONTENT)
    def clear(name: str) -> None:
        _known(name)
        registry.clear(name)

    @router.delete("", dependencies=guard, status_code=status.HTTP_204_NO_CONTENT)
    def clear_all() -> None:
        registry.clear_all()

    return router


def mount_if_enabled(
    include: Callable[[APIRouter], None], registry: FaultRegistry, token: str | None
) -> bool:
    """Mount the admin router only when a token is configured."""
    if not token:
        logger.info("fault admin API disabled (no FAULT_ADMIN_TOKEN)")
        return False
    include(admin_router(registry, token))
    return True


__all__ = ["FaultRegistry", "FaultSpec", "admin_router", "mount_if_enabled"]
