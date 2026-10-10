"""Close incidents created by integration tests (via the API, so closure is audited).

    uv run python scripts/close_test_incidents.py

Test incidents are recognised by their alert names (ApiTest*, DupTest*, InvTest*, ...).
"""

from __future__ import annotations

import re
import sys

import httpx

API = "http://127.0.0.1:18000"
TEST_ALERT = re.compile(r":(ApiTest|DupTest|InvTest|Redeliver|Resolve|Replay|SmokeTest)\w*$")
CLOSABLE = {"NEEDS_HUMAN", "REJECTED", "FAILED", "AWAITING_APPROVAL"}


def main() -> int:
    tok = httpx.post(
        f"{API}/api/v1/auth/dev-token", json={"username": "oscar.operator"}, timeout=10
    ).json()["access_token"]
    c = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {tok}"}, timeout=10)
    closed = 0
    for inc in c.get("/api/v1/incidents", params={"limit": 200}).json()["items"]:
        if TEST_ALERT.search(inc["fingerprint"]) and inc["status"] in CLOSABLE:
            r = c.post(
                f"/api/v1/incidents/{inc['id']}/resolve",
                json={"reason": "integration test data cleanup"},
            )
            closed += r.status_code == 200
    print(f"closed {closed} test incident(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
