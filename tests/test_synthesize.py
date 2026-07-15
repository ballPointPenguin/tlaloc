"""Tests for briefing construction, including the pattern-continuity block."""

from datetime import datetime, timezone

from tlaloc.sources import SourceReport
from tlaloc.synthesize import build_briefing

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def make_report(status: str = "ok") -> SourceReport:
    report = SourceReport(
        key="surface",
        title="Surface Analysis",
        kind="image",
        credit="NOAA Weather Prediction Center",
    )
    if status == "ok":
        report.summary = "High over the Plains."
    else:
        report.fail("network down")
    return report


def make_history(date_str: str, headline: str = "Ridge holds") -> dict:
    return {
        "date": date_str,
        "synthesis": {
            "headline": headline,
            "narrative": f"Full narrative for {date_str}.",
            "climate_context": f"Climate context for {date_str}.",
            "regional_notes": "",
        },
    }


class TestBuildBriefing:
    def test_includes_source_summaries_and_failures(self):
        briefing = build_briefing([make_report(), make_report("failed")], NOW)
        assert "High over the Plains." in briefing
        assert "[UNAVAILABLE — network down]" in briefing

    def test_no_history_block_without_records(self):
        for records in (None, []):
            briefing = build_briefing([make_report()], NOW, records)
            assert "Recent Tlaloc analyses" not in briefing

    def test_yesterday_gets_full_narrative_older_days_headline_only(self):
        records = [
            make_history("2026-07-14", "Block forming"),
            make_history("2026-07-13", "Progressive flow"),
            make_history("2026-07-12", "Zonal regime"),
        ]
        briefing = build_briefing([make_report()], NOW, records)
        assert "Recent Tlaloc analyses" in briefing
        assert "Yesterday (2026-07-14): Block forming" in briefing
        assert "Full narrative for 2026-07-14." in briefing
        assert "2 days ago (2026-07-13): Progressive flow" in briefing
        assert "Full narrative for 2026-07-13." not in briefing
        assert "3 days ago (2026-07-12): Zonal regime" in briefing

    def test_gap_days_are_labeled_by_actual_age(self):
        # A missed run means the newest record can be older than yesterday.
        briefing = build_briefing([make_report()], NOW, [make_history("2026-07-11")])
        assert "4 days ago (2026-07-11): Ridge holds" in briefing
        assert "Full narrative for 2026-07-11." in briefing
