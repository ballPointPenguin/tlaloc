"""Tests for the pure logic in tlaloc.sources (no network access)."""

from datetime import datetime, timezone

import pytest

from tlaloc.fetching import SourceError
from tlaloc.sources import (
    MCD_MAX_PRODUCTS,
    PRE_BLOCK_RE,
    filter_recent_mcd_entries,
    iter_airmass_candidate_urls,
    resolve_500mb_chart_url,
)


class TestResolve500mbChartUrl:
    NOW = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)

    def test_returns_most_recent_synoptic_time_when_available(self):
        url = resolve_500mb_chart_url(self.NOW, probe=lambda url: True)
        assert url.endswith("US500.20260715.12.gif")

    def test_steps_back_through_12h_synoptic_times(self):
        probed: list[str] = []

        def probe(url: str) -> bool:
            probed.append(url)
            return len(probed) == 3  # only the third candidate exists

        url = resolve_500mb_chart_url(self.NOW, probe=probe)
        assert url.endswith("US500.20260714.12.gif")
        assert probed[0].endswith("US500.20260715.12.gif")
        assert probed[1].endswith("US500.20260715.00.gif")

    def test_morning_run_starts_at_00z(self):
        morning = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
        url = resolve_500mb_chart_url(morning, probe=lambda url: True)
        assert url.endswith("US500.20260715.00.gif")

    def test_raises_source_error_when_nothing_found(self):
        with pytest.raises(SourceError):
            resolve_500mb_chart_url(self.NOW, probe=lambda url: False)


class TestIterAirmassCandidateUrls:
    NOW = datetime(2026, 7, 15, 14, 37, 42, tzinfo=timezone.utc)

    def test_latest_jpg_offered_first_per_satellite(self):
        urls = [url for url, _sat in iter_airmass_candidate_urls(self.NOW)]
        assert urls[0].endswith("GOES19/ABI/CONUS/AirMass/latest.jpg")
        assert any(url.endswith("GOES16/ABI/CONUS/AirMass/latest.jpg") for url in urls)

    def test_timestamped_candidates_use_julian_day_stamp(self):
        urls = [url for url, _sat in iter_airmass_candidate_urls(self.NOW)]
        # July 15 is day 196 of 2026; the first stamped candidate is the
        # 14:30 scan published at :31.
        assert "20261961431_GOES19-ABI-CONUS-AirMass-2500x1500.jpg" in urls[1]

    def test_no_candidate_is_in_the_future(self):
        for url, _sat in iter_airmass_candidate_urls(self.NOW):
            if url.endswith("latest.jpg"):
                continue
            stamp = url.rsplit("/", 1)[1].split("_", 1)[0]
            assert datetime.strptime(stamp, "%Y%j%H%M").replace(tzinfo=timezone.utc) <= self.NOW

    def test_satellites_ordered_newest_first(self):
        sats = [sat for _url, sat in iter_airmass_candidate_urls(self.NOW)]
        assert sats[0] == "GOES19"
        assert sats.index("GOES16") == sats.count("GOES19")

    def test_full_disk_sector_uses_fd_path_and_size(self):
        urls = [url for url, _sat in iter_airmass_candidate_urls(self.NOW, sector="FD")]
        assert urls[0].endswith("GOES19/ABI/FD/AirMass/latest.jpg")
        assert "20261961431_GOES19-ABI-FD-AirMass-1808x1808.jpg" in urls[1]
        assert not any("CONUS" in url for url in urls)


class TestFilterRecentMcdEntries:
    NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)

    @staticmethod
    def entry(issued: str, id_suffix: str = "x") -> dict:
        return {"issuanceTime": issued, "@id": f"https://api.weather.gov/products/{id_suffix}"}

    def test_keeps_only_entries_within_lookback(self):
        entries = [
            self.entry("2026-07-15T17:30:00+00:00", "fresh"),
            self.entry("2026-07-15T11:00:00+00:00", "stale"),
        ]
        recent = filter_recent_mcd_entries(entries, self.NOW)
        assert [e["@id"].rsplit("/", 1)[1] for e in recent] == ["fresh"]

    def test_caps_at_max_products(self):
        entries = [self.entry("2026-07-15T17:00:00Z", str(i)) for i in range(10)]
        assert len(filter_recent_mcd_entries(entries, self.NOW)) == MCD_MAX_PRODUCTS

    def test_skips_malformed_entries(self):
        entries = [
            {"issuanceTime": "not-a-date", "@id": "u"},
            {"@id": "missing-time"},
            {"issuanceTime": "2026-07-15T17:00:00Z"},  # missing @id
        ]
        assert filter_recent_mcd_entries(entries, self.NOW) == []


class TestPreBlockRegex:
    def test_extracts_product_text_from_nhc_page(self):
        html = "<html><body><PRE>\nABNT20 KNHC\nTropical outlook body\n</PRE></body></html>"
        match = PRE_BLOCK_RE.search(html)
        assert match is not None
        assert "Tropical outlook body" in match.group(1)

    def test_no_match_without_pre_block(self):
        assert PRE_BLOCK_RE.search("<html><body>nothing here</body></html>") is None
