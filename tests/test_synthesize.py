"""Tests for briefing construction, including the pattern-continuity block."""

from datetime import datetime, timezone

from tlaloc.sources import SourceReport
from types import SimpleNamespace

from tlaloc.synthesize import WORD_LIMITS, build_briefing, synthesize

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

    def test_yesterday_gets_lead_sentence_older_days_headline_only(self):
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

    def test_yesterday_contributes_only_its_first_sentence(self):
        record = make_history("2026-07-14")
        record["synthesis"]["narrative"] = "Ridge holds over the West. Second point. Third point."
        briefing = build_briefing([make_report()], NOW, [record])
        assert "Ridge holds over the West." in briefing
        assert "Second point" not in briefing
        assert "Climate context for" not in briefing


def publish_response(**overrides):
    fields = {
        "headline": "Ridge holds",
        "narrative": "Short narrative.",
        "regional_notes": "",
        "climate_context": "Neutral ENSO.",
    } | overrides
    block = SimpleNamespace(type="tool_use", id="t1", name="publish_synthesis", input=fields)
    return SimpleNamespace(stop_reason="tool_use", content=[block])


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages = self

    def stream(self, **kwargs):
        self.calls += 1
        return _FakeStream(self.responses.pop(0))


class _FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class TestLengthBudget:
    LONG = " ".join(["word"] * (WORD_LIMITS["narrative"] + 1))

    def test_over_budget_draft_is_sent_back_then_accepted(self):
        client = FakeClient([publish_response(narrative=self.LONG), publish_response()])
        result = synthesize(client, [make_report()])
        assert client.calls == 2
        assert result.narrative == "Short narrative."

    def test_persistent_overrun_is_published_rather_than_failing(self):
        client = FakeClient([publish_response(narrative=self.LONG)] * 3)
        result = synthesize(client, [make_report()])
        assert client.calls == 3
        assert result.narrative == self.LONG
