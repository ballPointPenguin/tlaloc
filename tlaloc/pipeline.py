"""Pipeline orchestration: collect -> interpret -> synthesize -> render -> archive."""

from datetime import datetime, timezone

import anthropic

from . import history
from .config import ARCHIVE_DIR, INDEX_HTML, MIN_IMAGE_SOURCES, MIN_TOTAL_SOURCES
from .interpret import interpret_all
from .render import write_archive_index, write_archive_page, write_index_html
from .sources import collect_all
from .synthesize import synthesize


def print_status(reports) -> None:
    for report in reports:
        marker = "ok " if report.status == "ok" else "FAIL"
        detail = report.display_url or ""
        print(f"  [{marker}] {report.key:<16} {report.title}  {detail}")
        if report.error:
            print(f"         {report.error}")


def run(collect_only: bool = False) -> int:
    print("Collecting sources...")
    reports = collect_all()
    print_status(reports)

    if collect_only:
        failed = [r for r in reports if r.status != "ok"]
        return 1 if len(failed) == len(reports) else 0

    client = anthropic.Anthropic()

    print("Interpreting sources...")
    interpret_all(client, reports)
    for report in reports:
        if report.status == "ok" and report.summary:
            print(f"  {report.key}: {report.summary[:120]}...")
        elif report.status != "ok":
            print(f"  {report.key}: FAILED — {report.error}")

    ok = [r for r in reports if r.status == "ok" and r.summary]
    ok_images = [r for r in ok if r.kind == "image"]
    if len(ok_images) < MIN_IMAGE_SOURCES or len(ok) < MIN_TOTAL_SOURCES:
        print(
            f"Aborting without publishing: only {len(ok)} sources "
            f"({len(ok_images)} image) survived; need >= {MIN_TOTAL_SOURCES} "
            f"({MIN_IMAGE_SOURCES} image). Existing page left untouched."
        )
        return 1

    # Prior analyses give the synthesist pattern-continuity context; loading
    # them is best-effort and must never block today's run.
    history_records: list[dict] = []
    try:
        today = datetime.now(timezone.utc).date()
        history_records = history.load_recent_records(3, before=today)
        if history_records:
            dates = ", ".join(r["date"] for r in history_records)
            print(f"Loaded prior analyses for continuity: {dates}")
    except Exception as exc:  # noqa: BLE001 — continuity context is optional
        print(f"  [warn] could not load history records: {exc}")

    print("Synthesizing...")
    synthesis = synthesize(client, reports, history_records)
    print(f"  headline: {synthesis.headline}")

    generated_at = datetime.now(timezone.utc)
    write_index_html(INDEX_HTML, synthesis, reports, generated_at)
    print(f"Wrote {INDEX_HTML}")

    # History and archive are derived artifacts: a failure here must not fail
    # the run, since the synthesis page (the product) is already written.
    try:
        record = history.record_from_run(synthesis, reports, generated_at)
        record_path = history.save_day_record(record)
        print(f"Wrote {record_path}")
        page_path = write_archive_page(ARCHIVE_DIR, record)
        write_archive_index(ARCHIVE_DIR, history.list_all_records())
        print(f"Wrote {page_path} and archive index")
    except Exception as exc:  # noqa: BLE001 — archive is best-effort by design
        print(f"  [warn] history/archive step failed (page already published): {exc}")

    return 0
