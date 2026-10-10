import math
import os
from dataclasses import dataclass


def flag(name: str) -> bool:
    value = os.getenv(name, "false").lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Settings:
    store: str = "memory"
    production: bool = False
    api_token: str = ""
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://localhost:3000")
    ai_timeout: float = 5
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    vertex: bool = False
    project: str = ""
    location: str = "us-central1"
    collection: str = "microgrid"
    document: str = "simulation"

    def __post_init__(self):
        if self.store not in {"memory", "firestore"}:
            raise ValueError("BACKEND_STORE must be memory or firestore")
        if self.production and not self.api_token:
            raise ValueError("BACKEND_PRODUCTION requires BACKEND_API_TOKEN")
        if not math.isfinite(self.ai_timeout) or not 0.1 <= self.ai_timeout <= 30:
            raise ValueError("BACKEND_AI_TIMEOUT_SECONDS must be between 0.1 and 30")
        if (self.vertex or self.store == "firestore") and not self.project:
            raise ValueError("Google Cloud configuration requires GOOGLE_CLOUD_PROJECT")
        if any("/" in value or not value for value in (self.collection, self.document)):
            raise ValueError("Firestore collection/document must be single path segments")
        if self.production and "*" in self.cors_origins:
            raise ValueError("Production CORS requires explicit origins")

    @classmethod
    def from_env(cls):
        return cls(
            store=os.getenv("BACKEND_STORE", "memory"),
            production=flag("BACKEND_PRODUCTION"),
            api_token=os.getenv("BACKEND_API_TOKEN", ""),
            cors_origins=tuple(x.strip() for x in os.getenv(
                "BACKEND_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
            ).split(",") if x.strip()),
            ai_timeout=float(os.getenv("BACKEND_AI_TIMEOUT_SECONDS", "5")),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            vertex=flag("GOOGLE_GENAI_USE_VERTEXAI"),
            project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            collection=os.getenv("FIRESTORE_COLLECTION", "microgrid"),
            document=os.getenv("FIRESTORE_DOCUMENT", "simulation"),
        )
