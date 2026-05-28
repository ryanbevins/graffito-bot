"""Record progress_snapshots rows from report.json — powers the dashboard graph."""

from __future__ import annotations

from pathlib import Path

from . import db, report
from .config import SETTINGS


def record(source: str, commit_sha: str | None = None, path: Path | None = None) -> int | None:
    """Read report.json and insert a row. source ∈ {pre_tick, post_tick, periodic, boot}.

    Returns the snapshot id, or None if report.json is missing.
    """
    rj = path or SETTINGS.report_json
    if not rj.exists():
        return None
    summary = report.load(rj)
    return db.insert_snapshot(
        fuzzy_match_pct=summary.fuzzy_match_pct,
        matched_code=summary.matched_code,
        total_code=summary.total_code,
        matched_functions=summary.matched_functions,
        total_functions=summary.total_functions,
        complete_units=summary.complete_units,
        total_units=summary.total_units,
        complete_code=summary.complete_code,
        commit_sha=commit_sha,
        source=source,
    )


def load_summary(path: Path | None = None) -> report.ReportSummary | None:
    rj = path or SETTINGS.report_json
    if not rj.exists():
        return None
    return report.load(rj)
