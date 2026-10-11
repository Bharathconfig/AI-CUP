from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

Event = Literal["normal", "storm", "outage"]
Tier = Literal["full", "essential", "critical"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Disruption(StrictModel):
    event: Literal["storm", "outage"]


class ImageAnalysisRequest(StrictModel):
    gcs_uri: str = Field(pattern=r"^gs://[^/]+/.+", max_length=2048)


class Bid(StrictModel):
    preferred_load_tier: Tier = "full"
    reserve_pct: float = 10
    export_kw: float = 4
    message: str = Field(default="Deterministic household bid (no AI credentials required).", max_length=500)


class Household(StrictModel):
    id: str
    name: str
    solar_kw: float
    nominal_solar_kw: float
    full_load_kw: float
    load_kw: float
    battery_pct: float
    battery_kwh: float
    battery_capacity_kwh: float = 10
    battery_max_kw: float = 4
    active_load_tier: Tier = "full"
    status: Literal["exporting", "importing", "balanced", "shedding"] = "balanced"
    served_kwh: float = 0
    unserved_kwh: float = 0
    shed_kwh: float = 0
    battery_charge_kwh: float = 0
    battery_discharge_kwh: float = 0
    grid_import_kwh: float = 0
    grid_export_kwh: float = 0
    curtailed_kwh: float = 0


class Trade(StrictModel):
    seller_id: str
    buyer_id: str
    energy_kwh: float = Field(gt=0)
    source: Literal["solar", "battery"]


class Totals(StrictModel):
    solar_kw: float = 0
    load_kw: float = 0
    battery_kwh: float = 0
    unserved_kwh: float = 0
    served_kwh: float = 0
    shed_kwh: float = 0
    battery_charge_kwh: float = 0
    battery_discharge_kwh: float = 0
    grid_import_kwh: float = 0
    grid_export_kwh: float = 0
    curtailed_kwh: float = 0


class State(StrictModel):
    generation_id: str = Field(default_factory=lambda: str(uuid4()))
    revision: int = Field(ge=0)
    event: Event
    grid_connected: bool
    updated_at: str
    simulation_minutes: Literal[15] = 15
    households: list[Household]
    trades: list[Trade] = Field(default_factory=list)
    totals: Totals


class Log(StrictModel):
    id: str
    timestamp: str
    household_id: str | None
    agent: str
    source: Literal["gemini", "deterministic"]
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Snapshot(StrictModel):
    state: State
    logs: list[Log] = Field(default_factory=list)
