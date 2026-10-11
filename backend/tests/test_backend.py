import asyncio
import math
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.agent import Agents
from backend.cloud_services import CloudServices
from backend.config import Settings
from backend.main import create_app
from backend.models import Bid
from backend.coordinator import HOURS, dispatch, initial
from backend.store import Conflict, FirestoreStore, MemoryStore


def run(coro):
    return asyncio.run(coro)


def round_for(snapshot, event, bid=None):
    return dispatch(snapshot, event, {h.id: bid or Bid() for h in snapshot.state.households}, [])


def assert_physics(before, after):
    state = after.state
    t = state.totals
    assert t.solar_kw * HOURS + t.battery_discharge_kwh + t.grid_import_kwh == pytest.approx(
        t.served_kwh + t.battery_charge_kwh + t.grid_export_kwh + t.curtailed_kwh
    )
    assert t.load_kw * HOURS == pytest.approx(t.served_kwh + t.unserved_kwh)
    assert t.battery_kwh == pytest.approx(
        before.state.totals.battery_kwh + t.battery_charge_kwh - t.battery_discharge_kwh
    )
    for old, h in zip(before.state.households, state.households):
        assert 0 <= h.battery_kwh <= h.battery_capacity_kwh
        assert 0 <= h.battery_pct <= 100
        assert h.battery_discharge_kwh <= h.battery_max_kw * HOURS + 1e-9
        assert h.battery_charge_kwh <= h.battery_max_kw * HOURS + 1e-9
        assert not (h.battery_charge_kwh > 1e-9 and h.battery_discharge_kwh > 1e-9)
        incoming = sum(x.energy_kwh for x in state.trades if x.buyer_id == h.id)
        outgoing = sum(x.energy_kwh for x in state.trades if x.seller_id == h.id)
        assert h.solar_kw * HOURS + h.battery_discharge_kwh + h.grid_import_kwh + incoming == pytest.approx(
            h.served_kwh + h.battery_charge_kwh + h.grid_export_kwh + h.curtailed_kwh + outgoing
        )
        assert h.battery_kwh == pytest.approx(old.battery_kwh + h.battery_charge_kwh - h.battery_discharge_kwh)
        assert all(getattr(h, field) >= -1e-9 for field in (
            "served_kwh", "unserved_kwh", "shed_kwh", "curtailed_kwh"
        ))
    assert all(x.energy_kwh > 0 and x.seller_id != x.buyer_id for x in state.trades)
    if state.event == "outage":
        assert t.grid_import_kwh == t.grid_export_kwh == 0


@pytest.mark.parametrize("event", ["storm", "outage"])
def test_repeated_rounds_conserve_and_persist(event):
    current = initial()
    first_battery = current.state.totals.battery_kwh
    for i in range(60):
        after = round_for(current, event)
        assert_physics(current, after)
        assert after.state.revision == i + 1
        current = after
    assert current.state.totals.battery_kwh != first_battery
    assert all(h.battery_kwh >= h.battery_capacity_kwh * 0.1 - 1e-9 for h in current.state.households)


def test_storm_not_compounded_and_outage_restores_solar():
    a = round_for(initial(), "storm")
    b = round_for(a, "storm")
    assert a.state.totals.solar_kw == b.state.totals.solar_kw
    c = round_for(b, "outage")
    assert c.state.totals.solar_kw == initial().state.totals.solar_kw


def test_zero_resources_sheds_and_reports_unserved():
    before = initial()
    for h in before.state.households:
        h.nominal_solar_kw = 0
        h.battery_kwh = 0
        h.battery_pct = 0
    before.state.totals.battery_kwh = 0
    after = round_for(before, "outage")
    assert_physics(before, after)
    assert after.state.totals.served_kwh == 0
    assert after.state.totals.unserved_kwh > 0
    assert all(h.active_load_tier == "critical" and h.status == "shedding" for h in after.state.households)


def test_surplus_curtailed_without_overcharging():
    before = initial()
    for h in before.state.households:
        h.nominal_solar_kw = 100
        h.battery_kwh = 10
        h.battery_pct = 100
    before.state.totals.battery_kwh = 40
    after = round_for(before, "outage")
    assert_physics(before, after)
    assert after.state.totals.curtailed_kwh > 0


def test_extreme_ai_bids_cannot_create_energy():
    before = initial()
    after = round_for(before, "outage", Bid(reserve_pct=-1e20, export_kw=1e20))
    assert_physics(before, after)
    assert all(h.battery_kwh >= 1 for h in after.state.households)
    with pytest.raises(ValidationError):
        Bid(reserve_pct=math.nan)


def test_randomized_physical_envelopes():
    import random
    rng = random.Random(112)
    for _ in range(100):
        before = initial()
        for h in before.state.households:
            h.battery_kwh = rng.uniform(0, 10)
            h.battery_pct = h.battery_kwh * 10
            h.nominal_solar_kw = rng.uniform(0, 12)
            h.full_load_kw = rng.uniform(0, 20)
        before.state.totals.battery_kwh = sum(h.battery_kwh for h in before.state.households)
        event = rng.choice(["storm", "outage"])
        after = round_for(before, event, Bid(
            reserve_pct=rng.uniform(-100, 200), export_kw=rng.uniform(-10, 20),
            preferred_load_tier=rng.choice(["full", "essential", "critical"]),
        ))
        assert_physics(before, after)


def test_http_contract_auth_validation_reset_and_logs():
    with TestClient(create_app(Settings(api_token="test-only"))) as client:
        assert client.get("/health").json() == {"status": "ok"}
        first = client.get("/api/state").json()
        datetime.fromisoformat(first["updated_at"])
        assert first["event"] == "normal" and first["simulation_minutes"] == 15
        assert client.post("/api/disruptions", json={"event": "storm"}).status_code == 401
        headers = {"X-API-Key": "test-only"}
        assert client.post("/api/disruptions", json={"event": "normal"}, headers=headers).status_code == 422
        assert client.post("/api/disruptions", json={"event": "outage", "other": 1}, headers=headers).status_code == 422
        assert client.post("/api/disruptions", content="bad", headers=headers).status_code == 422
        state = client.post("/api/disruptions", json={"event": "outage"}, headers=headers).json()
        assert state["revision"] == 1 and not state["grid_connected"]
        logs = client.get("/api/logs").json()["logs"]
        assert len(logs) == 5
        assert all(x["source"] == "deterministic" for x in logs)
        assert "no AI credentials" in logs[0]["message"]
        assert client.post("/api/reset").status_code == 401
        reset = client.post("/api/reset", headers=headers).json()
        assert reset["revision"] == 2 and reset["event"] == "normal"
        assert reset["households"] == first["households"]


def test_no_key_defaults_and_cors():
    with TestClient(create_app(Settings(cors_origins=("https://example.test",)))) as client:
        assert client.post("/api/disruptions", json={"event": "storm"}).status_code == 200
        response = client.options("/api/disruptions", headers={
            "Origin": "https://example.test", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-API-Key",
        })
        assert response.headers["access-control-allow-origin"] == "https://example.test"
        assert client.options("/api/disruptions", headers={
            "Origin": "https://evil.test", "Access-Control-Request-Method": "POST",
        }).status_code == 400


@pytest.mark.parametrize("kwargs", [
    {"production": True}, {"store": "bad"}, {"ai_timeout": float("nan")},
    {"ai_timeout": 0}, {"vertex": True}, {"store": "firestore"},
    {"collection": "../bad"}, {"production": True, "api_token": "test", "cors_origins": ("*",)},
])
def test_configuration_fail_closed(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("BACKEND_API_TOKEN", "test")
    monkeypatch.setenv("BACKEND_PRODUCTION", "true")
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "https://one.test, https://two.test")
    settings = Settings.from_env()
    assert settings.production and settings.api_token == "test"
    assert settings.cors_origins == ("https://one.test", "https://two.test")


def test_deployment_configuration_and_canonical_precedence(monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "firestore")
    monkeypatch.setenv("REQUIRE_API_TOKEN", "true")
    monkeypatch.setenv("API_TOKEN", "deployment-test-value")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("BACKEND_STORE", "memory")
    monkeypatch.setenv("BACKEND_API_TOKEN", "legacy-test-value")
    settings = Settings.from_env()
    assert settings.store == "firestore" and settings.production
    assert settings.api_token == "deployment-test-value"
    assert settings.vertex and settings.project == "test-project"
    assert settings.location == "global" and settings.gemini_model == "gemini-2.5-flash"
    monkeypatch.setenv("API_TOKEN", "")
    with pytest.raises(ValueError, match="requires API_TOKEN"):
        Settings.from_env()


def test_optional_cloud_integration_configuration(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BIGQUERY_TABLE", "test-project.microgrid.dispatches")
    monkeypatch.setenv("GCS_BUCKET", "microgrid-test")
    monkeypatch.setenv("PUBSUB_TOPIC", "dispatches")
    monkeypatch.setenv("EARTH_ENGINE_PROJECT", "test-earth-project")
    settings = Settings.from_env()
    assert settings.bigquery_table == "test-project.microgrid.dispatches"
    assert settings.storage_bucket == "microgrid-test"
    assert settings.pubsub_topic == "dispatches"
    assert settings.earth_engine_project == "test-earth-project"
    with pytest.raises(ValueError, match="require GOOGLE_CLOUD_PROJECT"):
        Settings(bigquery_table="project.dataset.table")
    with pytest.raises(ValueError, match="topic ID"):
        Settings(project="test-project", pubsub_topic="projects/test/topics/name")


def test_cloud_services_record_committed_snapshot_to_configured_sinks():
    class BigQuery:
        rows = None

        def insert_rows_json(self, table, rows, row_ids):
            self.rows = (table, rows, row_ids)
            return []

    class Blob:
        def __init__(self):
            self.upload = None

        def upload_from_string(self, data, content_type):
            self.upload = (data, content_type)

    class Storage:
        def __init__(self):
            self.object = Blob()

        def bucket(self, name):
            assert name == "microgrid-test"
            return self

        def blob(self, name):
            assert name.endswith("/1.json")
            return self.object

    class Publisher:
        def __init__(self):
            self.message = None

        def topic_path(self, project, topic):
            return f"projects/{project}/topics/{topic}"

        def publish(self, topic, message):
            self.message = (topic, message)
            return SimpleNamespace(result=lambda timeout: None)

    services = CloudServices.__new__(CloudServices)
    services.settings = SimpleNamespace(
        bigquery_table="project.dataset.table", storage_bucket="microgrid-test",
        pubsub_topic="dispatches", project="test-project",
    )
    services.bigquery = BigQuery()
    services.storage = Storage()
    services.publisher = Publisher()
    run(services.record(initial(1)))
    assert services.bigquery.rows[0] == "project.dataset.table"
    assert services.bigquery.rows[1][0]["revision"] == 1
    assert services.storage.object.upload[1] == "application/json"
    assert services.publisher.message[0] == "projects/test-project/topics/dispatches"


def test_cloud_service_endpoints_are_validated_and_optional():
    class Cloud:
        async def record(self, snapshot):
            pass

        async def analyze_image(self, uri):
            if not uri.startswith("gs://accepted/"):
                raise ValueError("wrong bucket")
            return [{"description": "solar panel", "score": 0.9}]

        async def solar_context(self, latitude, longitude):
            return {"latitude": latitude, "longitude": longitude}

    with TestClient(create_app(Settings(api_token="test-token"), cloud_services=Cloud())) as client:
        headers = {"X-API-Key": "test-token"}
        assert client.post("/api/images/analyze", json={
            "gcs_uri": "gs://accepted/panel.jpg",
        }, headers=headers).json()["labels"][0]["description"] == "solar panel"
        assert client.post("/api/images/analyze", json={
            "gcs_uri": "gs://other/panel.jpg",
        }, headers=headers).status_code == 422
        assert client.get("/api/geospatial/solar?latitude=1&longitude=2").status_code == 401
        assert client.get("/api/geospatial/solar?latitude=91&longitude=0",
                          headers=headers).status_code == 422
        assert client.get("/api/geospatial/solar?latitude=1&longitude=2",
                          headers=headers).json() == {
            "latitude": 1.0, "longitude": 2.0,
        }


def test_reset_revisions_are_strictly_monotonic():
    with TestClient(create_app()) as client:
        for expected in range(1, 7):
            route = "/api/reset" if expected % 2 else "/api/disruptions"
            kwargs = {} if route.endswith("reset") else {"json": {"event": "storm"}}
            response = client.post(route, **kwargs)
            assert response.status_code == 200
            assert response.json()["revision"] == expected
            assert client.get("/api/state").json()["revision"] == expected


@pytest.mark.parametrize("mode", ["valid", "invalid", "error", "timeout"])
def test_ai_structured_output_and_graceful_fallback(mode):
    async def scenario():
        agents = Agents(Settings(ai_timeout=0.1))
        async def generate(**kwargs):
            assert kwargs["config"]["response_schema"] is Bid
            if mode == "timeout":
                await asyncio.sleep(1)
            if mode == "error":
                raise RuntimeError("sensitive upstream text")
            return SimpleNamespace(parsed=(
                {"preferred_load_tier": "essential", "reserve_pct": -100,
                 "export_kw": 100, "message": "Structured AI bid"}
                if mode == "valid" else {"unknown": "bad"}
            ), text=None)
        agents.client = SimpleNamespace(aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=generate)
        ))
        bids, logs = await agents.bids(initial(), "outage")
        assert len(bids) == len(logs) == 4
        assert all(x.source == ("gemini" if mode == "valid" else "deterministic") for x in logs)
        assert all("sensitive" not in x.message for x in logs)
        assert all(10 <= b.reserve_pct <= 90 and 0 <= b.export_kw <= 4 for b in bids.values())
    run(scenario())


def test_ai_initialization_failure_falls_back(monkeypatch):
    from google import genai
    def fail(**kwargs):
        raise RuntimeError("private credential detail")
    monkeypatch.setattr(genai, "Client", fail)
    agents = Agents(Settings(gemini_api_key="dummy-test-value"))
    bids, logs = run(agents.bids(initial(), "storm"))
    assert len(bids) == 4
    assert all(x.source == "deterministic" and "configuration unavailable" in x.message for x in logs)
    assert all("private credential" not in x.message for x in logs)


def test_logs_bounded():
    async def scenario():
        agents = Agents(Settings())
        snapshot = initial()
        for _ in range(50):
            bids, logs = await agents.bids(snapshot, "outage")
            snapshot = dispatch(snapshot, "outage", bids, logs)
        assert len(snapshot.logs) == 200
        assert len({entry.id for entry in snapshot.logs}) == 200
    run(scenario())


def test_memory_compare_and_set_and_detached_reads():
    async def scenario():
        store = MemoryStore()
        a = await store.read()
        a.state.households[0].battery_kwh = 0
        assert (await store.read()).state.households[0].battery_kwh == 7.5
        updated = initial(1)
        await store.commit(0, updated)
        updated.state.revision = 100
        assert (await store.read()).state.revision == 1
        with pytest.raises(Conflict):
            await store.commit(0, initial(1))
    run(scenario())


def test_local_mutations_serialize():
    # Run simultaneous HTTP requests through the same lifespan/event loop.
    import httpx
    async def requests():
        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                responses = await asyncio.gather(*(
                    client.post("/api/disruptions", json={"event": "storm"}) for _ in range(5)
                ))
                assert sorted(r.json()["revision"] for r in responses) == [1, 2, 3, 4, 5]
                assert (await client.get("/api/state")).json()["revision"] == 5
    run(requests())


def test_safe_storage_errors_and_conflicts():
    for error, code in [(RuntimeError("secret"), 503), (Conflict(), 409)]:
        store = MemoryStore()
        store.commit = AsyncMock(side_effect=error)
        with TestClient(create_app(store=store)) as client:
            response = client.post("/api/reset")
            assert response.status_code == code and "secret" not in response.text
    store = MemoryStore()
    store.read = AsyncMock(side_effect=RuntimeError("secret"))
    with TestClient(create_app(store=store)) as client:
        assert client.get("/health").status_code == 503
        assert client.get("/api/state").json() == {"detail": "State store unavailable"}


def test_firestore_atomic_cas_uses_shared_document(monkeypatch):
    import google.cloud.firestore_v1 as firestore
    from copy import deepcopy

    async def scenario():
        lock = asyncio.Lock()
        shared = {}
        class Ref:
            async def get(self, transaction=None):
                return SimpleNamespace(exists=bool(shared), to_dict=lambda: deepcopy(shared))
            async def create(self, value):
                if shared:
                    raise Conflict()
                shared.update(deepcopy(value))
        class Transaction:
            def set(self, ref, value):
                shared.clear()
                shared.update(deepcopy(value))
        def transactional(fn):
            async def wrapped(transaction):
                async with lock:
                    return await fn(transaction)
            return wrapped
        monkeypatch.setattr(firestore, "async_transactional", transactional)
        stores = []
        for _ in range(2):
            store = FirestoreStore.__new__(FirestoreStore)
            store.initial_snapshot = initial()
            store.ref = Ref()
            store.client = SimpleNamespace(transaction=Transaction)
            stores.append(store)
        assert (await stores[0].read()).state.revision == 0
        results = await asyncio.gather(*(
            store.commit(0, initial(1)) for store in stores
        ), return_exceptions=True)
        assert sum(isinstance(x, Conflict) for x in results) == 1
        assert set(shared) == {"state", "logs"}
        assert (await stores[1].read()).state.revision == 1
    run(scenario())


def test_generation_survives_round_and_reset():
    with TestClient(create_app(Settings())) as client:
        original = client.get("/api/state").json()
        after = client.post("/api/disruptions", json={"event": "storm"}).json()
        reset = client.post("/api/reset").json()
        assert original["generation_id"] == after["generation_id"] == reset["generation_id"]
        assert reset["revision"] > after["revision"]
    assert initial().state.generation_id != initial().state.generation_id
