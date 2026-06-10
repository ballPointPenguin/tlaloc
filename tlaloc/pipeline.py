"""Pipeline orchestration: collect -> interpret -> synthesize -> render."""

import anthropic

from .config import INDEX_HTML, MIN_IMAGE_SOURCES, MIN_TOTAL_SOURCES
from .interpret import interpret_all
from .render import write_index_html
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

    print("Synthesizing...")
    synthesis = synthesize(client, reports)
    print(f"  headline: {synthesis.headline}")

    write_index_html(INDEX_HTML, synthesis, reports)
    print(f"Wrote {INDEX_HTML}")
    return 0
