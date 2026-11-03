"""Deterministic Quality / Verdict / Priority. The LLM gives the 0-5 scores; this file decides."""
from __future__ import annotations

from dataclasses import dataclass

VERDICTS = ["Skip", "Skim", "Solid", "Gem"]               # index = level
VERDICT_LABEL = {"Gem": "💎 Gem", "Solid": "✅ Solid", "Skim": "👀 Skim", "Skip": "🗑 Skip"}
LABEL_TO_VERDICT = {v: k for k, v in VERDICT_LABEL.items()}
PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


def plain_verdict(label: str | None) -> str | None:
    """'💎 Gem' -> 'Gem', 'Gem' -> 'Gem', None -> None."""
    if not label:
        return None
    if label in LABEL_TO_VERDICT:
        return LABEL_TO_VERDICT[label]
    for v in VERDICTS:
        if v.lower() in label.lower():
            return v
    return None


@dataclass
class Result:
    quality: float
    score: float
    verdict: str        # plain: Gem / Solid / Skim / Skip
    priority: str       # P1..P4
    note: str = ""      # extra text for the "Why" field


def _clamp(x, lo=0, hi=5) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def quality(timeless, substance, credibility, red_flags, serious: list[str]) -> float:
    base = 2 * (_clamp(timeless) + _clamp(substance) + _clamp(credibility)) / 3
    penalty = sum(1 for f in (red_flags or []) if f in serious)
    return round(max(0.0, base - penalty), 1)


def compute(j: dict, cfg: dict) -> Result:
    sc = cfg["scoring"]
    q = quality(j.get("timeless"), j.get("substance"), j.get("credibility"), j.get("red_flags"), sc["serious_red_flags"])
    rel, act = _clamp(j.get("relevance")), _clamp(j.get("actionability"))
    w = sc["weights"]
    score = round(w["quality"] * q / 10 + w["relevance"] * rel / 5 + w["actionability"] * act / 5, 3)

    v = sc["verdict"]
    if q >= v["gem_quality"] and rel >= v["gem_min_relevance"]:
        verdict = "Gem"
    elif q >= v["solid_quality"]:
        verdict = "Solid"
    elif q >= v["skim_quality"]:
        verdict = "Skim"
    else:
        verdict = "Skip"

    p = sc["priority"]
    if verdict == "Skip":
        priority = "P4"
    elif score >= p["p1"]:
        priority = "P1"
    elif score >= p["p2"]:
        priority = "P2"
    elif score >= p["p3"]:
        priority = "P3"
    else:
        priority = "P4"

    note = ""
    llm = plain_verdict(j.get("verdict_suggestion"))
    if llm and abs(VERDICTS.index(llm) - VERDICTS.index(verdict)) > 1:
        note = f"(Judge suggested {llm}; scores say {verdict}.)"
    return Result(q, score, verdict, priority, note)


def score_from_props(quality_v, relevance, actionability, cfg) -> float:
    """Recompute the ranking score from stored Notion numbers (used by planner/digest)."""
    w = cfg["scoring"]["weights"]
    return round(w["quality"] * (quality_v or 0) / 10 + w["relevance"] * (relevance or 0) / 5
                 + w["actionability"] * (actionability or 0) / 5, 3)
