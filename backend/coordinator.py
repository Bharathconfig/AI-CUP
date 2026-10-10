"""A single 15-minute dispatch; kW is power and kWh is interval energy.

Ideal, lossless batteries and unconstrained local wiring are prototype assumptions.
AI preferences never authorize extra energy, excessive battery power, or grid use
in an outage. Solar is served locally, shared, then stored/exported/curtailed.
"""
from uuid import uuid4

from .models import Bid, Household, Log, Snapshot, State, Totals, Trade, now

HOURS = 0.25
FACTORS = {"full": 1.0, "essential": 0.6, "critical": 0.3}
TIERS = list(FACTORS)


def total(households: list[Household]) -> Totals:
    return Totals(**{
        field: sum(getattr(h, field) for h in households)
        for field in Totals.model_fields
    })


def initial(revision: int = 0) -> Snapshot:
    households = [
        Household(id=f"house-{i + 1}", name=name, solar_kw=solar,
                  nominal_solar_kw=solar, full_load_kw=load, load_kw=load,
                  battery_pct=pct, battery_kwh=pct / 10)
        for i, (name, solar, load, pct) in enumerate([
            ("Solar Haven", 7, 2, 75), ("Family Home", 2, 5, 55),
            ("Garden Cottage", 4, 2.5, 65), ("Community Clinic", 1, 6, 80),
        ])
    ]
    return Snapshot(state=State(revision=revision, event="normal",
                               grid_connected=True, updated_at=now(),
                               households=households, totals=total(households)))


def log(household_id, agent, source, message, payload) -> Log:
    return Log(id=str(uuid4()), timestamp=now(), household_id=household_id,
               agent=agent, source=source, message=message, payload=payload)


def dispatch(previous: Snapshot, event: str, bids: dict[str, Bid],
             bid_logs: list[Log]) -> Snapshot:
    state = previous.state.model_copy(deep=True)
    state.revision += 1
    state.event = event
    state.grid_connected = event != "outage"
    state.updated_at = now()
    state.trades = []
    houses = state.households
    solar, remaining, capacity, export_limits = {}, {}, {}, {}
    for h in houses:
        bid = bids[h.id]
        reserve = min(90, max(10, bid.reserve_pct)) / 100 * h.battery_capacity_kwh
        h.solar_kw = h.nominal_solar_kw * (0.25 if event == "storm" else 1)
        h.active_load_tier = bid.preferred_load_tier
        solar[h.id] = h.solar_kw * HOURS
        capacity[h.id] = min(h.battery_max_kw * HOURS, max(0, h.battery_kwh - reserve))
        export_limits[h.id] = min(h.battery_max_kw, max(0, bid.export_kw)) * HOURS
        for field in ("served_kwh", "unserved_kwh", "shed_kwh", "battery_charge_kwh",
                      "battery_discharge_kwh", "grid_import_kwh", "grid_export_kwh",
                      "curtailed_kwh"):
            setattr(h, field, 0)
    if not state.grid_connected:
        # Prefer reducing flexible households before the clinic. This is a
        # deterministic aggregate heuristic, not an optimal network solver.
        available = sum(solar.values()) + sum(capacity.values())
        for tier in ("essential", "critical"):
            for h in houses:
                demand = sum(x.full_load_kw * FACTORS[x.active_load_tier] * HOURS for x in houses)
                if demand <= available + 1e-9:
                    break
                if TIERS.index(h.active_load_tier) < TIERS.index(tier):
                    h.active_load_tier = tier
    for h in houses:
        h.load_kw = h.full_load_kw * FACTORS[h.active_load_tier]
        remaining[h.id] = h.load_kw * HOURS
        own = min(remaining[h.id], solar[h.id])
        remaining[h.id] -= own
        solar[h.id] -= own
        h.served_kwh += own
        h.shed_kwh = (h.full_load_kw - h.load_kw) * HOURS

    def transfer(seller, buyer, amount, source):
        if amount <= 1e-9:
            return
        remaining[buyer.id] -= amount
        buyer.served_kwh += amount
        state.trades.append(Trade(seller_id=seller.id, buyer_id=buyer.id,
                                  energy_kwh=amount, source=source))

    for seller in houses:
        for buyer in houses:
            if seller.id != buyer.id:
                amount = min(solar[seller.id], remaining[buyer.id], export_limits[seller.id])
                transfer(seller, buyer, amount, "solar")
                solar[seller.id] -= amount
                export_limits[seller.id] -= amount
    for h in houses:
        amount = min(remaining[h.id], capacity[h.id])
        remaining[h.id] -= amount
        capacity[h.id] -= amount
        h.served_kwh += amount
        h.battery_discharge_kwh += amount
    # Share battery power only while islanded; grid-connected rounds do not
    # drain neighbors' reserves merely to avoid an unlimited utility import.
    if not state.grid_connected:
        for seller in houses:
            for buyer in houses:
                if seller.id != buyer.id:
                    amount = min(capacity[seller.id], remaining[buyer.id], export_limits[seller.id])
                    transfer(seller, buyer, amount, "battery")
                    capacity[seller.id] -= amount
                    export_limits[seller.id] -= amount
                    seller.battery_discharge_kwh += amount
    for h in houses:
        if state.grid_connected:
            h.grid_import_kwh = remaining[h.id]
            h.served_kwh += remaining[h.id]
        else:
            h.unserved_kwh = remaining[h.id]
        # No charging and discharging within the same finite interval.
        if h.battery_discharge_kwh <= 1e-9:
            h.battery_charge_kwh = min(solar[h.id], h.battery_max_kw * HOURS,
                                       h.battery_capacity_kwh - h.battery_kwh)
        solar[h.id] -= h.battery_charge_kwh
        if state.grid_connected:
            h.grid_export_kwh = solar[h.id]
        else:
            h.curtailed_kwh = solar[h.id]
        h.battery_kwh += h.battery_charge_kwh - h.battery_discharge_kwh
        h.battery_pct = h.battery_kwh / h.battery_capacity_kwh * 100
        exporting = h.grid_export_kwh + sum(t.energy_kwh for t in state.trades if t.seller_id == h.id)
        importing = h.grid_import_kwh + sum(t.energy_kwh for t in state.trades if t.buyer_id == h.id)
        h.status = ("shedding" if h.shed_kwh + h.unserved_kwh > 1e-9 else
                    "exporting" if exporting > 1e-9 else
                    "importing" if importing > 1e-9 else "balanced")
    state.totals = total(houses)
    coordinator_log = log(None, "coordinator", "deterministic",
                          "Completed one 15-minute physics-constrained dispatch; ideal lossless batteries.",
                          {"revision": state.revision, "event": event,
                           "trades": [trade.model_dump() for trade in state.trades],
                           "totals": state.totals.model_dump()})
    return Snapshot(state=state, logs=(previous.logs + bid_logs + [coordinator_log])[-200:])
