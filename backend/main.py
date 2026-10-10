import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agents import Agents
from .config import Settings
from .models import Disruption, State
from .simulation import dispatch, initial, log
from .store import Conflict, FirestoreStore, MemoryStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, store=None, agents=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = store or (FirestoreStore(settings) if settings.store == "firestore" else MemoryStore())
        app.state.agents = agents or Agents(settings)
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

    @app.post("/api/disruptions", response_model=State, dependencies=[Depends(authorize)])
    async def disruption(body: Disruption):
        return await mutate(body.event)

    @app.post("/api/reset", response_model=State, dependencies=[Depends(authorize)])
    async def reset():
        return await mutate()

    return app


app = create_app()
