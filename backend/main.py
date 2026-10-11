"""Run from repository root: uvicorn backend.main:app --port 8000.

GETs are read-only. Every disruption POST advances exactly 15 minutes and
persists battery energy; reset restores the baseline and advances revision.
Normal is the reset baseline, storm reduces solar to 25% with utility supply,
and outage islands the community. No background clock or real device control.
Initial/reset totals describe inventory, not a dispatched interval.
Interval unserved_kwh counts unmet active demand; shed_kwh separately counts
deliberately disabled flexible demand. Add both for unmet full demand.
Memory is process-local and resets on restart; use one worker or Firestore
for shared durable state. Cross-instance conflicts return 409; retry explicitly.
"""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agent import Agents
from .cloud_services import CloudServices
from .config import Settings
from .models import Disruption, ImageAnalysisRequest, State
from .coordinator import dispatch, initial, log
from .store import Conflict, FirestoreStore, MemoryStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, store=None, agents=None, cloud_services=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = store or (FirestoreStore(settings) if settings.store == "firestore" else MemoryStore())
        app.state.agents = agents or Agents(settings)
        app.state.cloud_services = cloud_services or CloudServices(settings)
        app.state.run_lock = asyncio.Lock()
        try:
            yield
        finally:
            await app.state.agents.close()
            await app.state.store.close()

    app = FastAPI(title="Community Microgrid Prototype", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_credentials=False, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "X-API-Key"])

    async def authorize(x_api_key: str | None = Header(default=None)):
        if settings.api_token and not secrets.compare_digest(
            (x_api_key or "").encode(), settings.api_token.encode()
        ):
            raise HTTPException(401, "Invalid API key")

    async def read():
        try:
            return await app.state.store.read()
        except Exception:
            logger.error("State store read unavailable")
            raise HTTPException(503, "State store unavailable") from None

    async def mutate(event=None):
        async with app.state.run_lock:
            previous = await read()
            if event is None:
                updated = initial(previous.state.revision + 1)
                updated.state.generation_id = previous.state.generation_id
                updated.logs = [log(None, "coordinator", "deterministic",
                                    "Simulation reset to initial conditions.", {"revision": updated.state.revision})]
            else:
                bids, entries = await app.state.agents.bids(previous, event)
                updated = dispatch(previous, event, bids, entries)
            try:
                await app.state.store.commit(previous.state.revision, updated)
            except Conflict:
                raise HTTPException(409, "Simulation changed concurrently; retry the request") from None
            except Exception:
                logger.error("State store commit unavailable")
                raise HTTPException(503, "State store unavailable") from None
            await app.state.cloud_services.record(updated)
            return updated.state

    @app.get("/health")
    async def health():
        await read()
        return {"status": "ok"}

    @app.get("/api/state", response_model=State)
    async def state():
        return (await read()).state

    @app.get("/api/logs")
    async def logs():
        return {"logs": (await read()).logs}

    @app.post("/api/images/analyze", dependencies=[Depends(authorize)])
    async def analyze_image(body: ImageAnalysisRequest):
        try:
            labels = await app.state.cloud_services.analyze_image(body.gcs_uri)
            return {"labels": labels}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError:
            raise HTTPException(503, "Image analysis unavailable") from None
        except Exception:
            logger.warning("Image analysis unavailable")
            raise HTTPException(503, "Image analysis unavailable") from None

    @app.get("/api/geospatial/solar", dependencies=[Depends(authorize)])
    async def solar_context(latitude: float, longitude: float):
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise HTTPException(422, "Latitude or longitude is out of range")
        try:
            return await app.state.cloud_services.solar_context(latitude, longitude)
        except Exception:
            logger.warning("Earth Engine solar context unavailable")
            raise HTTPException(503, "Earth Engine solar context unavailable") from None

    @app.post("/api/disruptions", response_model=State, dependencies=[Depends(authorize)])
    async def disruption(body: Disruption):
        return await mutate(body.event)

    @app.post("/api/reset", response_model=State, dependencies=[Depends(authorize)])
    async def reset():
        return await mutate()

    return app


app = create_app()
