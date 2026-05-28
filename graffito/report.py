"""Parse build/GMSJ01/report.json into typed summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class UnitInfo:
    name: str
    fuzzy_pct: float
    total_code: int
    matched_code: int
    matched_functions: int
    total_functions: int
    complete: bool
    source_path: str


@dataclass
class ReportSummary:
    fuzzy_match_pct: float
    total_code: int
    matched_code: int
    complete_code: int
    matched_functions: int
    total_functions: int
    total_units: int
    complete_units: int
    units: list[UnitInfo]

    def overall_summary(self) -> dict[str, Any]:
        return {
            "fuzzy_match_pct": self.fuzzy_match_pct,
            "matched_code": self.matched_code,
            "total_code": self.total_code,
            "complete_code": self.complete_code,
            "matched_functions": self.matched_functions,
            "total_functions": self.total_functions,
            "complete_units": self.complete_units,
            "total_units": self.total_units,
        }

    def top_nonmatching(self, n: int = 20) -> list[UnitInfo]:
        """Largest non-100%-matching units by total_code."""
        candidates = [u for u in self.units if u.fuzzy_pct < 100.0]
        candidates.sort(key=lambda u: u.total_code, reverse=True)
        return candidates[:n]

    def by_source_path(self) -> dict[str, UnitInfo]:
        return {u.source_path: u for u in self.units if u.source_path}


def _as_int(v: Any) -> int:
    if v is None:
        return 0
    return int(v)


def load(path: Path) -> ReportSummary:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    measures = data.get("measures", {})
    units_raw = data.get("units", [])
    units: list[UnitInfo] = []
    for u in units_raw:
        m = u.get("measures", {})
        meta = u.get("metadata", {}) or {}
        units.append(
            UnitInfo(
                name=u.get("name", "?"),
                fuzzy_pct=float(m.get("fuzzy_match_percent", 0.0) or 0.0),
                total_code=_as_int(m.get("total_code")),
                matched_code=_as_int(m.get("matched_code")),
                matched_functions=_as_int(m.get("matched_functions")),
                total_functions=_as_int(m.get("total_functions")),
                complete=bool(meta.get("complete", False)),
                source_path=str(meta.get("source_path", "")),
            )
        )
    return ReportSummary(
        fuzzy_match_pct=float(measures.get("fuzzy_match_percent", 0.0) or 0.0),
        total_code=_as_int(measures.get("total_code")),
        matched_code=_as_int(measures.get("matched_code")),
        complete_code=_as_int(measures.get("complete_code")),
        matched_functions=_as_int(measures.get("matched_functions")),
        total_functions=_as_int(measures.get("total_functions")),
        total_units=_as_int(measures.get("total_units")),
        complete_units=_as_int(measures.get("complete_units")),
        units=units,
    )


def diff_unit_pcts(before: ReportSummary, after: ReportSummary) -> list[tuple[str, float, float]]:
    """Return [(unit_name, before_pct, after_pct)] for units that changed."""
    bmap = {u.name: u.fuzzy_pct for u in before.units}
    out: list[tuple[str, float, float]] = []
    for u in after.units:
        b = bmap.get(u.name)
        if b is None:
            if u.fuzzy_pct != 0.0:
                out.append((u.name, 0.0, u.fuzzy_pct))
        elif abs(b - u.fuzzy_pct) > 1e-6:
            out.append((u.name, b, u.fuzzy_pct))
    return out
