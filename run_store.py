"""
run_store.py – Persist and load URL-check runs to disk
=======================================================
Each run is saved as a JSON file in a `runs/` directory:
  {
    "id": "20260220_120035",
    "timestamp": "2026-02-20 12:00:35",
    "source": "https://example.com/sitemap.xml",
    "url_count": 84,
    "settings": { ... },
    "results": [ ... ]   ← list of row dicts
  }
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path(__file__).parent / "runs"


def _ensure_dir() -> None:
    RUNS_DIR.mkdir(exist_ok=True)


def save_run(
    source: str,
    results: list[dict],
    settings: dict | None = None,
) -> str:
    """Save a run and return its ID."""
    _ensure_dir()
    now = datetime.now()
    run_id = now.strftime("%Y%m%d_%H%M%S")
    data = {
        "id": run_id,
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "url_count": len(results),
        "settings": settings or {},
        "results": results,
    }
    path = RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return run_id


def list_runs() -> list[dict]:
    """Return a list of run summaries (without results), newest first."""
    _ensure_dir()
    runs = []
    for f in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            runs.append({
                "id": data["id"],
                "timestamp": data["timestamp"],
                "source": data.get("source", "?"),
                "url_count": data.get("url_count", 0),
                "filename": f.name,
            })
        except Exception:
            continue
    return runs


def load_run(run_id: str) -> dict | None:
    """Load a full run (including results) by ID."""
    _ensure_dir()
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def delete_run(run_id: str) -> bool:
    """Delete a saved run by ID."""
    path = RUNS_DIR / f"{run_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False
