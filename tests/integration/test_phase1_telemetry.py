"""Phase 1 exit gate against the live stack.

Run with: make up && make test-integration
Injects scenario A, waits for metric/log/trace/alert, then always clears the fault.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKOUT = f"http://127.0.0.1:{os.environ.get('CHECKOUT_PORT', '18081')}"

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario_a() -> Iterator[None]:
    try:
        httpx.get(f"{CHECKOUT}/healthz", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("stack not running (make up)")
    inject = ROOT / "scripts" / "inject-fault.sh"
    subprocess.run([str(inject), "checkout-5xx", "0.8"], check=True)
    try:
        yield
    finally:
        subprocess.run([str(inject), "clear"], check=False)


def test_fault_produces_metric_log_trace_and_alert(scenario_a: None) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_telemetry.py"), "--timeout", "360"],
        capture_output=True,
        text=True,
        check=False,
    )
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
