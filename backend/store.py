import asyncio

from .config import Settings
from .models import Snapshot
from .coordinator import initial


class Conflict(Exception):
    pass


class MemoryStore:
    def __init__(self):
        self.snapshot = initial()
        self.lock = asyncio.Lock()

    async def read(self) -> Snapshot:
        async with self.lock:
            return self.snapshot.model_copy(deep=True)

    async def commit(self, expected: int, snapshot: Snapshot):
        async with self.lock:
            if self.snapshot.state.revision != expected:
                raise Conflict()
            self.snapshot = snapshot.model_copy(deep=True)

    async def close(self):
        pass


class FirestoreStore:
    """State and bounded logs share one document and one transactional CAS."""

    def __init__(self, settings: Settings):
        from google.cloud.firestore_v1 import AsyncClient
        self.client = AsyncClient(project=settings.project)
        self.ref = self.client.collection(settings.collection).document(settings.document)
        self.initial_snapshot = initial()

    async def read(self) -> Snapshot:
        document = await self.ref.get()
        if not document.exists:
            try:
                await self.ref.create(self.initial_snapshot.model_dump())
            except Exception:
                document = await self.ref.get()
                if not document.exists:
                    raise
                return Snapshot.model_validate(document.to_dict())
            return self.initial_snapshot.model_copy(deep=True)
        data = document.to_dict()
        data["state"].setdefault("generation_id", "firestore-existing")
        return Snapshot.model_validate(data)

    async def commit(self, expected: int, snapshot: Snapshot):
        from google.cloud.firestore_v1 import async_transactional

        @async_transactional
        async def compare_and_set(transaction):
            document = await self.ref.get(transaction=transaction)
            revision = document.to_dict()["state"]["revision"] if document.exists else 0
            if revision != expected:
                raise Conflict()
            transaction.set(self.ref, snapshot.model_dump())

        await compare_and_set(self.client.transaction())

    async def close(self):
        self.client.close()
