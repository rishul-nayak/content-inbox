"""Pick today's checklist. Pure function, so it is easy to test."""
from __future__ import annotations

from scoring import PRIORITY_RANK

DEFAULT_MIN = 10  # if an item has no time estimate


def plan_day(candidates: list[dict], already: list[dict], budget: int, cfg: dict, weekday: str = "") -> list[str]:
    """
    candidates / already: dicts with keys id, priority, score, time, value_type.
    `already` = undone items already planned for today (kept; they use up budget).
    Returns the ids of NEW items to plan for today.
    """
    pc = cfg["planner"]
    long_min, max_long = pc["long_item_min"], pc["max_long_items_per_day"]
    over_ok = weekday in (pc.get("long_item_over_budget_days") or [])

    def t(it):
        return it.get("time") or DEFAULT_MIN

    used = sum(t(i) for i in already)
    longs = sum(1 for i in already if t(i) > long_min)
    has_action = any(i.get("value_type") == "Action" for i in already)
    chosen: list[str] = []

    ordered = sorted(candidates, key=lambda i: (PRIORITY_RANK.get(i.get("priority"), 5), -(i.get("score") or 0)))

    def try_add(it) -> bool:
        nonlocal used, longs
        if it["id"] in chosen:
            return False
        is_long = t(it) > long_min
        if is_long and longs >= max_long:
            return False
        if used + t(it) > budget and not (is_long and over_ok and used < budget):
            return False
        chosen.append(it["id"])
        used += t(it)
        longs += is_long
        return True

    if pc.get("ensure_action_item") and not has_action:
        for it in ordered:
            if it.get("value_type") == "Action" and try_add(it):
                break
    for it in ordered:
        try_add(it)
    return chosen
