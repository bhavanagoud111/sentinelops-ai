from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
from demo_checkout.app import (
    FAULT_BAD_CONFIG,
    CheckoutSettings,
    ConfigError,
    create_app,
    validate_payment_provider,
)
from demo_faultkit import FaultRegistry, FaultSpec
from fastapi.testclient import TestClient

Handler = Callable[[httpx.Request], httpx.Response]


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"sku": "SKU-001", "reserved": 1, "remaining": 9})


@pytest.fixture
def faults() -> FaultRegistry:
    return FaultRegistry([FAULT_BAD_CONFIG])


@pytest.fixture
def make_client(faults: FaultRegistry) -> Iterator[Callable[[Handler], TestClient]]:
    clients: list[TestClient] = []

    def _make(handler: Handler) -> TestClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://inv")
        app = create_app(CheckoutSettings(), http_client=http, faults=faults)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def test_checkout_success(make_client: Callable[[Handler], TestClient]) -> None:
    resp = make_client(_ok).post("/checkout", json={"sku": "SKU-001", "quantity": 1})
    assert resp.status_code == 201
    assert resp.json()["status"] == "placed"


def test_bad_config_fault_returns_500(
    make_client: Callable[[Handler], TestClient], faults: FaultRegistry
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok(request)

    client = make_client(handler)
    faults.activate(FAULT_BAD_CONFIG, FaultSpec(probability=1.0))
    resp = client.post("/checkout", json={"sku": "SKU-001"})
    assert resp.status_code == 500
    assert calls == [], "inventory must not be called when config is invalid"

    faults.clear(FAULT_BAD_CONFIG)
    assert client.post("/checkout", json={"sku": "SKU-001"}).status_code == 201


def test_out_of_stock_maps_to_409(make_client: Callable[[Handler], TestClient]) -> None:
    client = make_client(lambda r: httpx.Response(409, json={"detail": "insufficient"}))
    assert client.post("/checkout", json={"sku": "SKU-001"}).status_code == 409


def test_inventory_error_maps_to_502(make_client: Callable[[Handler], TestClient]) -> None:
    client = make_client(lambda r: httpx.Response(500))
    assert client.post("/checkout", json={"sku": "SKU-001"}).status_code == 502


def test_inventory_timeout_maps_to_504(make_client: Callable[[Handler], TestClient]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert make_client(handler).post("/checkout", json={"sku": "SKU-001"}).status_code == 504


def test_inventory_unreachable_maps_to_502(make_client: Callable[[Handler], TestClient]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert make_client(handler).post("/checkout", json={"sku": "SKU-001"}).status_code == 502


@pytest.mark.parametrize(
    "body", [{"sku": "bad"}, {"sku": "SKU-001", "quantity": 0}, {"sku": "SKU-001", "quantity": 101}]
)
def test_validation(make_client: Callable[[Handler], TestClient], body: dict[str, object]) -> None:
    assert make_client(_ok).post("/checkout", json=body).status_code == 422


@pytest.mark.parametrize(
    "url", ["payments.internal:::", "ftp://x", "http://", "", "javascript:alert(1)"]
)
def test_validate_payment_provider_rejects(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_payment_provider(url)


def test_validate_payment_provider_accepts() -> None:
    assert validate_payment_provider("https://pay.example/v1") == "https://pay.example/v1"


def test_payment_timeout_after_retries_maps_to_504(
    make_client: Callable[[Handler], TestClient],
) -> None:
    payment_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/payments"):
            payment_calls.append(request)
            raise httpx.ReadTimeout("slow provider", request=request)
        return _ok(request)

    assert make_client(handler).post("/checkout", json={"sku": "SKU-001"}).status_code == 504
    assert len(payment_calls) == 3, "default policy retries payment three times"


def test_payment_recovers_on_retry(make_client: Callable[[Handler], TestClient]) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/payments"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ReadTimeout("blip", request=request)
            return httpx.Response(200, json={"payment_id": "p", "status": "authorized"})
        return _ok(request)

    assert make_client(handler).post("/checkout", json={"sku": "SKU-001"}).status_code == 201


def test_payment_error_maps_to_502(make_client: Callable[[Handler], TestClient]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/payments"):
            return httpx.Response(503)
        return _ok(request)

    assert make_client(handler).post("/checkout", json={"sku": "SKU-001"}).status_code == 502


def test_malformed_provider_url_setting_fails_every_request() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_ok))
    settings = CheckoutSettings(PAYMENT_PROVIDER_URL="payments.internal:::")
    with TestClient(create_app(settings, http_client=http)) as client:
        assert client.post("/checkout", json={"sku": "SKU-001"}).status_code == 500
