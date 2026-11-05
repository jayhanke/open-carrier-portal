from __future__ import annotations

import json
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from open_carrier_portal.adapters.observium import ObserviumAdapter
from open_carrier_portal.admin_store import AdminStore
from open_carrier_portal.diagnostics import log_context
from open_carrier_portal.errors import (
    CapabilityNotSupportedError,
    ConnectorError,
    UpstreamResponseError,
)
from open_carrier_portal.repository import Repository

pytestmark = pytest.mark.anyio


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    value = Repository(tmp_path / "test.db", "k" * 32)
    value.initialize()
    return value


async def test_maps_devices_and_keeps_token_in_authorization_header(
    repository: Repository,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer observium-secret"
        assert "observium-secret" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "count": 1,
                "devices": {
                    "277": {
                        "device_id": "277",
                        "hostname": "router.example",
                        "vendor": "Cisco",
                        "hardware": "CISCO1841",
                        "os": "ios",
                        "status": "1",
                        "last_polled": "2026-10-09 12:00:00",
                    }
                },
            },
        )

    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="observium-secret",
        repository=repository,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await adapter.list_devices(page=1, page_size=50)
    finally:
        await adapter.close()

    assert len(result.items) == 1
    assert result.items[0].name == "router.example"
    assert result.items[0].operational_status == "up"
    assert result.items[0].id.startswith("device_")
    assert "277" not in result.items[0].id


@pytest.mark.parametrize("shape", ["singular", "plural"])
async def test_port_detail_maps_reported_optical_power(repository, shape):
    record = {
        "port_id": 12, "device_id": 277, "ifName": "xe-0/0/1",
        "ifOperStatus": "up", "ifAdminStatus": "up", "ifSpeed": "10000000000",
        "sensor_rx_power": "-12.75", "sensor_tx_power": 0,
        "last_polled": "2026-11-05 19:30:00",
    }
    payload = {"port": record} if shape == "singular" else {"ports": {"12": record}}
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=payload)

    adapter = ObserviumAdapter(
        base_url="https://observium.example", api_token="private-token",
        repository=repository, transport=httpx.MockTransport(upstream),
    )
    try:
        port = await adapter.get_interface(
            repository.ocp_resource_id("observium", "interface", "12")
        )
    finally:
        await adapter.close()
    assert paths == ["/api/v0/ports/12/"]
    assert port.optical_rx_dbm == -12.75 and port.optical_tx_dbm == 0
    assert port.speed_bps == 10_000_000_000 and port.operational_status == "up"
    assert port.device_id == repository.ocp_resource_id("observium", "device", "277")


async def test_device_detail_accepts_single_record_response(repository):
    adapter = ObserviumAdapter(
        base_url="https://observium.example", api_token="private-token",
        repository=repository,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"device": {"device_id": 277, "hostname": "edge-a", "status": 1}}
            )
        ),
    )
    identifier = repository.ocp_resource_id("observium", "device", "277")
    try:
        device = await adapter.get_device(identifier)
    finally:
        await adapter.close()
    assert device.id == identifier and device.operational_status == "up"


@pytest.mark.parametrize("value", ["not-a-number", "NaN", "inf", True, {"invalid": "reading"}])
async def test_invalid_optical_reading_keeps_verified_port_status_and_logs_safely(
    repository, value, caplog
):
    adapter = ObserviumAdapter(
        base_url="https://observium.example", api_token="private-token", repository=repository,
        transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"port": {
                "port_id": 12, "device_id": 277, "ifName": "ethernet",
                "ifOperStatus": "up", "sensor_rx_power": value, "sensor_tx_power": -1.2,
            }},
        )),
    )
    try:
        port = await adapter.get_interface(
            repository.ocp_resource_id("observium", "interface", "12")
        )
    finally:
        await adapter.close()
    assert port.operational_status == "up"
    assert port.optical_rx_dbm is None and port.optical_tx_dbm == -1.2
    assert "response.optical_invalid" in caplog.text and "sensor_rx_power" in caplog.text
    assert "not-a-number" not in caplog.text and "private-token" not in caplog.text


@pytest.mark.parametrize(
    "status,event",
    [(404, "resource_missing"), (503, "upstream_failed"), (401, "credential_rejected")],
)
async def test_detail_http_errors_distinguish_record_lookup_from_global_connection_failure(
    repository, caplog, status, event
):
    adapter = ObserviumAdapter(
        base_url="https://observium.example", api_token="private-token", repository=repository,
        transport=httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    try:
        with pytest.raises(ConnectorError):
            await adapter.get_interface(
                repository.ocp_resource_id("observium", "interface", "12")
            )
    finally:
        await adapter.close()
    assert f"request.{event}" in caplog.text
    if status == 404:
        assert "mapped record ID and API route" in caplog.text
    assert "request.endpoint_missing" not in caplog.text


async def test_rejects_malformed_upstream_shape(repository: Repository) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            content=json.dumps({"status": "ok", "devices": "not-a-collection"}),
        )
    )
    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="secret",
        repository=repository,
        transport=transport,
    )
    try:
        with pytest.raises(UpstreamResponseError):
            await adapter.list_devices(page=1, page_size=50)
    finally:
        await adapter.close()


async def test_numeric_metrics_are_not_fabricated(repository: Repository) -> None:
    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="secret",
        repository=repository,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    try:
        with pytest.raises(CapabilityNotSupportedError):
            await adapter.query_metrics(
                resource_id="interface_1",
                metric="bits_in",
                start=datetime(2026, 10, 9, tzinfo=UTC),
                end=datetime(2026, 10, 10, tzinfo=UTC),
                step_seconds=60,
            )
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    ("status", "event"),
    [
        (401, "request.credential_rejected"),
        (403, "request.credential_rejected"),
        (404, "request.endpoint_missing"),
        (429, "request.rate_limited"),
        (503, "request.upstream_failed"),
        (200, "response.invalid_json"),
    ],
)
async def test_health_failure_has_safe_actionable_log(repository, tmp_path, caplog, status, event):
    store = AdminStore(tmp_path / "logs.db")
    store.initialize()
    adapter = ObserviumAdapter(
        base_url="https://user:private-url-password@observium.example",
        api_token="private-api-token",
        repository=repository,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="private-upstream-response")
        ),
    )
    with log_context(store) as context:
        try:
            with pytest.raises(ConnectorError):
                await adapter.health_check()
        finally:
            await adapter.close()
    events = store.recent_events()
    assert events[0]["event"] == event
    assert events[0]["reference"] == context.request_id
    assert json.loads(events[0]["details"])["http_status"] == status
    for private in ("private-url-password", "private-api-token", "private-upstream-response"):
        assert private not in json.dumps(events)
        assert private not in caplog.text


@pytest.mark.parametrize(
    ("cause", "event"),
    [
        (ssl.SSLCertVerificationError(1, "private-error"), "request.tls_verification_failed"),
        (socket.gaierror(-2, "private-error"), "request.dns_failed"),
        (ConnectionRefusedError("private-error"), "request.connection_refused"),
    ],
)
async def test_transport_failure_keeps_safe_root_cause(repository, tmp_path, cause, event):
    def handler(_request):
        raise httpx.ConnectError("private-network-error") from cause

    store = AdminStore(tmp_path / "logs.db")
    store.initialize()
    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="private-api-token",
        repository=repository,
        transport=httpx.MockTransport(handler),
    )
    with log_context(store):
        try:
            with pytest.raises(ConnectorError):
                await adapter.health_check()
        finally:
            await adapter.close()
    events = store.recent_events("observium")
    assert events[0]["event"] == event
    assert "private-error" not in json.dumps(events)
    assert "private-network-error" not in json.dumps(events)


async def test_redirect_cannot_pass_health_check_even_with_success_json(repository, tmp_path):
    store = AdminStore(tmp_path / "logs.db")
    store.initialize()
    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="private-api-token",
        repository=repository,
        transport=httpx.MockTransport(lambda _: httpx.Response(302, json={"status": "ok"})),
    )
    with log_context(store):
        try:
            with pytest.raises(UpstreamResponseError):
                await adapter.health_check()
        finally:
            await adapter.close()
    assert store.recent_events("observium")[0]["event"] == "request.redirected"


@pytest.mark.parametrize("devices", [{}, [], {"277": {"device_id": "277"}}])
async def test_health_checks_device_api_without_requiring_openapi(repository, tmp_path, devices):
    store = AdminStore(tmp_path / "logs.db")
    store.initialize()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/monitoring/api/v0/devices/":
            return httpx.Response(200, json={"status": "ok", "devices": devices})
        return httpx.Response(404)

    adapter = ObserviumAdapter(
        base_url="https://observium.example/monitoring",
        api_token="private-api-token",
        repository=repository,
        transport=httpx.MockTransport(handler),
    )
    with log_context(store):
        try:
            await adapter.health_check()
        finally:
            await adapter.close()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert dict(requests[0].url.params) == {"pageno": "1", "pagesize": "1"}
    events = store.recent_events("observium")
    assert events[0]["event"] == "request.completed"
    assert json.loads(events[0]["details"])["operation"] == "health"
    assert any("GET api/v0/devices/" in event["message"] for event in events)


@pytest.mark.parametrize(
    "payload", [{}, {"status": "ok"}, {"devices": "invalid"}, {"devices": [1]}]
)
async def test_health_rejects_responses_without_valid_device_collection(repository, payload):
    adapter = ObserviumAdapter(
        base_url="https://observium.example",
        api_token="private-api-token",
        repository=repository,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(UpstreamResponseError):
            await adapter.health_check()
    finally:
        await adapter.close()
