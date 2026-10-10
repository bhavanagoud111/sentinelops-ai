"""Deployment-time allowlist configuration (SENTINEL_* env) for the runbook catalog."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

PROBE = (
    "from sentinel_policy import runbooks as r, RunbookId;"
    "print(r.TARGET_NAMESPACE, sorted(r.TARGET_DEPLOYMENTS), sorted(r.ALLOWED_FLAGS),"
    " r.CATALOG[RunbookId.SCALE_DEPLOYMENT].scale_bounds,"
    " sorted(r.CATALOG[RunbookId.RESTART_DEPLOYMENT].allowed_namespaces))"
)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SENTINEL_")}
    return subprocess.run(
        [sys.executable, "-c", PROBE], env={**clean, **env}, capture_output=True, text=True
    )


def test_defaults_are_the_demo_sandbox() -> None:
    out = _run({})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[0] == "sentinelops-demo"
    assert "'checkout'" in out.stdout
    assert "(1, 4)" in out.stdout


def test_custom_allowlist() -> None:
    out = _run(
        {
            "SENTINEL_TARGET_NAMESPACE": "shop",
            "SENTINEL_ALLOWED_DEPLOYMENTS": "web, api",
            "SENTINEL_ALLOWED_FLAGS": "FEATURE_NEW_CART",
            "SENTINEL_SCALE_MAX": "6",
        }
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "shop ['api', 'web'] ['FEATURE_NEW_CART'] (1, 6) ['shop']"


@pytest.mark.parametrize(
    "env",
    [
        {"SENTINEL_TARGET_NAMESPACE": "a,b"},  # exactly one namespace
        {"SENTINEL_TARGET_NAMESPACE": "Bad_NS"},
        {"SENTINEL_ALLOWED_DEPLOYMENTS": "web;rm -rf"},
        {"SENTINEL_ALLOWED_DEPLOYMENTS": " , "},  # empty
        {"SENTINEL_ALLOWED_FLAGS": "not_a_flag"},
        {"SENTINEL_SCALE_MAX": "50"},
    ],
)
def test_invalid_config_refuses_to_start(env: dict[str, str]) -> None:
    out = _run(env)
    assert out.returncode != 0
    assert "PolicyConfigError" in out.stderr or "ValueError" in out.stderr
