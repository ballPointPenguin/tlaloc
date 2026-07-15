"""Tests for per-day history records and the archive pages derived from them."""

import json
from datetime import date, datetime, timezone

from tlaloc.history import (
    list_all_records,
    load_recent_records,
    record_from_run,
    save_day_record,
)
from tlaloc.render import render_archive_index, render_archive_page, write_archive_page
from tlaloc.sources import SourceReport
from tlaloc.synthesize import Synthesis

GENERATED_AT = datetime(2026, 7, 15, 14, 0, 5, tzinfo=timezone.utc)


def make_reports() -> list[SourceReport]:
    ok_image = SourceReport(
        key="surface",
        title="Surface Analysis",
        kind="image",
        credit="NOAA Weather Prediction Center",
    )
    ok_image.display_url = "https://example.test/surface.gif"
    ok_image.image_base64 = "not-stored"
    ok_image.summary = "High over the Plains."
    ok_text = SourceReport(
        key="enso",
        title="ENSO State",
        kind="text",
        credit="NOAA Climate Prediction Center",
    )
    ok_text.raw_text = "long raw table"
    ok_text.summary = "Warm phase developing."
    failed = SourceReport(
        key="airmass", title="Air Mass RGB", kind="image", credit="NOAA NESDIS"
    ).fail("404 all candidates")
    return [ok_image, ok_text, failed]


def make_record() -> dict:
    synthesis = Synthesis(
        headline="Ridge holds",
        narrative="Para one.\n\nPara two.",
        climate_context="ENSO warm phase.",
    )
    return record_from_run(synthesis, make_reports(), GENERATED_AT)


class TestRecordFromRun:
    def test_shape_and_date(self):
        record = make_record()
        assert record["schema_version"] == 1
        assert record["date"] == "2026-07-15"
        assert record["generated_at"] == "2026-07-15T14:00:05Z"
        assert record["synthesis"]["regional_notes"] == ""

    def test_excludes_bulk_runtime_fields(self):
        blob = json.dumps(make_record())
        assert "not-stored" not in blob
        assert "long raw table" not in blob

    def test_failed_source_recorded_with_error(self):
        record = make_record()
        failed = next(s for s in record["sources"] if s["key"] == "airmass")
        assert failed["status"] == "failed"
        assert "404" in failed["error"]


class TestSaveAndLoad:
    def test_round_trip(self, tmp_path):
        record = make_record()
        path = save_day_record(record, data_dir=tmp_path)
        assert path.name == "2026-07-15.json"
        assert json.loads(path.read_text()) == record

    def test_load_recent_orders_and_filters(self, tmp_path):
        for day in ("2026-07-12", "2026-07-14", "2026-07-15"):
            record = make_record() | {"date": day}
            save_day_record(record, data_dir=tmp_path)
        recent = load_recent_records(5, before=date(2026, 7, 15), data_dir=tmp_path)
        assert [r["date"] for r in recent] == ["2026-07-14", "2026-07-12"]
        assert [r["date"] for r in load_recent_records(1, date(2026, 7, 15), tmp_path)] == [
            "2026-07-14"
        ]

    def test_corrupt_and_foreign_files_skipped(self, tmp_path):
        save_day_record(make_record(), data_dir=tmp_path)
        (tmp_path / "2026-07-14.json").write_text("{ not json")
        (tmp_path / "notes.json").write_text("{}")
        records = list_all_records(data_dir=tmp_path)
        assert [r["date"] for r in records] == ["2026-07-15"]

    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_recent_records(3, date(2026, 7, 15), tmp_path / "nope") == []


class TestArchiveRendering:
    def test_page_has_text_record_but_no_images(self, tmp_path):
        record = make_record()
        path = write_archive_page(tmp_path, record)
        html = path.read_text()
        assert "Ridge holds" in html
        assert "Para one." in html
        assert "High over the Plains." in html
        assert "Unavailable for this analysis." in html  # the failed source
        assert "<img" not in html
        assert "https://example.test/surface.gif" in html  # linked, not embedded

    def test_regional_notes_section_only_when_present(self):
        record = make_record()
        assert "Regional Signals" not in render_archive_page(record)
        record["synthesis"]["regional_notes"] = "MCD active over Kansas."
        html = render_archive_page(record)
        assert "Regional Signals" in html
        assert "MCD active over Kansas." in html

    def test_index_lists_records_newest_first(self):
        older = make_record() | {"date": "2026-07-14"}
        html = render_archive_index([make_record(), older])
        assert html.index("2026-07-15.html") < html.index("2026-07-14.html")
        assert "Ridge holds" in html
