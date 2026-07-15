"""Per-day history records: the durable, text-only archive of each analysis.

One JSON file per UTC run date (data/YYYY-MM-DD.json) is the single source of
truth for history. The archive HTML pages and the pattern-continuity context
fed back into synthesis are both derived from these records. Chart images are
deliberately not stored: most source URLs are overwritten or expire within
days, and the interpretation text already records what each chart showed.
"""

import json
import re
from datetime import date, datetime
from pathlib import Path

from .config import DATA_DIR
from .sources import SourceReport
from .synthesize import Synthesis

SCHEMA_VERSION = 1

DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def record_from_run(
    synthesis: Synthesis, reports: list[SourceReport], generated_at: datetime
) -> dict:
    """Build the day's record. Excludes runtime-only bulk (image data, raw text)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "date": generated_at.strftime("%Y-%m-%d"),
        "generated_at": generated_at.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "synthesis": {
            "headline": synthesis.headline,
            "narrative": synthesis.narrative,
            "climate_context": synthesis.climate_context,
            "regional_notes": getattr(synthesis, "regional_notes", ""),
        },
        "sources": [
            {
                "key": report.key,
                "title": report.title,
                "kind": report.kind,
                "credit": report.credit,
                "status": report.status,
                "display_url": report.display_url,
                "summary": report.summary,
                "error": report.error,
            }
            for report in reports
        ],
    }


def save_day_record(record: dict, data_dir: Path = DATA_DIR) -> Path:
    """Write the record as data/YYYY-MM-DD.json, overwriting any same-day run."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{record['date']}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return path


def _load_records_descending(data_dir: Path) -> list[dict]:
    """Yield valid records newest-first; a corrupt file is skipped, never fatal."""
    records = []
    if not data_dir.is_dir():
        return records
    for path in sorted(data_dir.glob("*.json"), reverse=True):
        if not DATE_FILE_RE.match(path.stem):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  [warn] skipping unreadable history record {path.name}: {exc}")
            continue
        if not isinstance(record, dict) or "synthesis" not in record:
            print(f"  [warn] skipping malformed history record {path.name}")
            continue
        records.append(record)
    return records


def load_recent_records(n: int, before: date, data_dir: Path = DATA_DIR) -> list[dict]:
    """The n most recent records strictly before `before`, newest first."""
    cutoff = before.strftime("%Y-%m-%d")
    recent = [r for r in _load_records_descending(data_dir) if r.get("date", "") < cutoff]
    return recent[:n]


def list_all_records(data_dir: Path = DATA_DIR) -> list[dict]:
    """Every stored record, newest first (for the archive index)."""
    return _load_records_descending(data_dir)
