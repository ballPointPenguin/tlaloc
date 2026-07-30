"""The core source registry and collectors.

Design note: the core sources are deliberately hardcoded. A daily synoptic
overview needs the same backbone every day — surface analysis, 500 mb
heights, water-vapor/airmass imagery, and the major center discussions — so
no model is asked to decide *whether* to fetch them. Dynamic, on-demand
source selection happens later, in the synthesis stage, where Claude gets a
small menu of supplementary products it can pull if the core data raises
questions (see synthesize.py).

Each collector returns a SourceReport. Collectors never raise: any failure is
captured on the report so the pipeline can degrade gracefully.
"""

import re
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal, Sequence
from urllib.parse import urljoin, urlsplit

from .config import MAX_TEXT_CHARS
from .fetching import SourceError, fetch_image_base64, fetch_json, fetch_text, image_url_exists

Kind = Literal["image", "text"]


@dataclass
class SourceReport:
    key: str
    title: str
    kind: Kind
    credit: str
    status: Literal["ok", "failed"] = "ok"
    error: str | None = None
    # Image sources
    display_url: str | None = None
    image_base64: str | None = None
    image_media_type: str | None = None
    # Text sources
    raw_text: str | None = None
    # Filled in by the interpretation stage
    summary: str | None = None

    def fail(self, error: str) -> "SourceReport":
        self.status = "failed"
        self.error = error
        return self


# ---------------------------------------------------------------------------
# Image sources
# ---------------------------------------------------------------------------

WPC_SURFACE_URL = "https://www.wpc.ncep.noaa.gov/sfc/namussfcwbg.gif"

COD_500MB_URL_TEMPLATE = "https://weather.cod.edu/wxdata/upper/US/500/US500.{date}.{hour}.gif"
COD_500MB_LOOKBACK_STEPS = 4  # 12-hour synoptic times to look back

GOES_AIRMASS_BASE = "https://cdn.star.nesdis.noaa.gov/{satellite}/ABI/{sector}/AirMass"
GOES_AIRMASS_FILENAME = "{stamp}_{satellite}-ABI-{sector}-AirMass-{size}.jpg"
# Image size variant per sector, chosen to stay under the 5 MB request cap.
GOES_SECTOR_SIZES = {"CONUS": "2500x1500", "FD": "1808x1808"}
GOES_SATELLITES = ("GOES19", "GOES16")
GOES_LOOKBACK_HOURS = 3


def resolve_first_available_image(
    candidates: Sequence[str],
    probe: Callable[[str], bool] = image_url_exists,
) -> str:
    """First candidate URL that serves image content.

    Used where a product's page is stable but its image filename is not
    guaranteed — probing a short list of known naming conventions survives an
    upstream rename without a code change.
    """
    for url in candidates:
        if probe(url):
            return url
    raise SourceError(f"None of {len(candidates)} candidate URLs served an image: {candidates[0]}")


def collect_surface_analysis() -> SourceReport:
    report = SourceReport(
        key="surface",
        title="Surface Analysis",
        kind="image",
        credit="NOAA Weather Prediction Center",
    )
    try:
        data, media_type = fetch_image_base64(WPC_SURFACE_URL)
    except SourceError as exc:
        return report.fail(str(exc))
    report.display_url = WPC_SURFACE_URL
    report.image_base64 = data
    report.image_media_type = media_type
    return report


def resolve_500mb_chart_url(
    now_utc: datetime | None = None,
    probe: Callable[[str], bool] = image_url_exists,
) -> str:
    """Find the most recent COD 500 mb analysis, stepping back 12 h at a time."""
    probe_time = now_utc or datetime.now(timezone.utc)
    hour = 12 if probe_time.hour >= 12 else 0
    aligned = probe_time.replace(hour=hour, minute=0, second=0, microsecond=0)
    for step in range(COD_500MB_LOOKBACK_STEPS):
        t = aligned - timedelta(hours=12 * step)
        url = COD_500MB_URL_TEMPLATE.format(date=t.strftime("%Y%m%d"), hour=t.strftime("%H"))
        if probe(url):
            return url
    raise SourceError(
        f"No COD 500mb chart found within {COD_500MB_LOOKBACK_STEPS} synoptic times of "
        f"{probe_time:%Y-%m-%dT%H:00Z}"
    )


def collect_500mb_analysis() -> SourceReport:
    report = SourceReport(
        key="upper_500mb",
        title="500 mb Analysis",
        kind="image",
        credit="College of DuPage NEXLAB",
    )
    try:
        url = resolve_500mb_chart_url()
        data, media_type = fetch_image_base64(url)
    except SourceError as exc:
        return report.fail(str(exc))
    report.display_url = url
    report.image_base64 = data
    report.image_media_type = media_type
    return report


def iter_airmass_candidate_urls(now_utc: datetime, sector: str = "CONUS"):
    """Yield candidate GOES Air Mass URLs for a sector, newest-first per satellite.

    Tries the stable latest.jpg first, then timestamped 10-minute scans
    (stamped :01 then :00, matching how recent products are published).
    """
    size = GOES_SECTOR_SIZES[sector]
    normalized = now_utc.astimezone(timezone.utc).replace(second=0, microsecond=0)
    aligned = normalized - timedelta(minutes=normalized.minute % 10)
    for satellite in GOES_SATELLITES:
        base = GOES_AIRMASS_BASE.format(satellite=satellite, sector=sector)
        yield f"{base}/latest.jpg", satellite
        for step in range(GOES_LOOKBACK_HOURS * 6):
            scan = aligned - timedelta(minutes=10 * step)
            for minute_offset in (1, 0):
                candidate = scan + timedelta(minutes=minute_offset)
                if candidate > normalized:
                    continue
                stamp = candidate.strftime("%Y%j%H%M")
                filename = GOES_AIRMASS_FILENAME.format(
                    stamp=stamp, satellite=satellite, sector=sector, size=size
                )
                yield f"{base}/{filename}", satellite


def _collect_airmass_sector(report: SourceReport, sector: str) -> SourceReport:
    now_utc = datetime.now(timezone.utc)
    for url, satellite in iter_airmass_candidate_urls(now_utc, sector):
        if not image_url_exists(url):
            continue
        try:
            data, media_type = fetch_image_base64(url)
        except SourceError:
            continue  # e.g. latest.jpg over the 5 MB limit; try the next candidate
        report.title = f"{report.title} ({satellite.replace('GOES', 'GOES-')})"
        report.display_url = url
        report.image_base64 = data
        report.image_media_type = media_type
        return report
    return report.fail(
        f"No usable GOES {sector} Air Mass image found within {GOES_LOOKBACK_HOURS}h lookback"
    )


def collect_airmass_rgb() -> SourceReport:
    report = SourceReport(
        key="airmass",
        title="Air Mass RGB",
        kind="image",
        credit="NOAA NESDIS / GOES East",
    )
    return _collect_airmass_sector(report, "CONUS")


def collect_airmass_fulldisk() -> SourceReport:
    """The Full Disk frame is the intentional Canada/Mexico/tropics view: one
    image spanning the Arctic to South America and both tropical basins."""
    report = SourceReport(
        key="airmass_fd",
        title="Air Mass RGB Full Disk",
        kind="image",
        credit="NOAA NESDIS / GOES East",
    )
    return _collect_airmass_sector(report, "FD")


ECCC_SURFACE_URL = "https://weather.gc.ca/data/analysis/947_100.gif"


def collect_eccc_surface_analysis() -> SourceReport:
    """The Canadian Meteorological Centre's own surface analysis: an independent
    center's take on the North American pattern, with better frontal detail
    over Canada than the US-centric WPC chart."""
    report = SourceReport(
        key="eccc_surface",
        title="Canadian Surface Analysis",
        kind="image",
        credit="Environment and Climate Change Canada",
    )
    try:
        data, media_type = fetch_image_base64(ECCC_SURFACE_URL)
    except SourceError as exc:
        return report.fail(str(exc))
    report.display_url = ECCC_SURFACE_URL
    report.image_base64 = data
    report.image_media_type = media_type
    return report


CPC_610DAY_TEMP_URL = (
    "https://www.cpc.ncep.noaa.gov/products/predictions/610day/610temp.new.gif"
)


def collect_cpc_610day_outlook() -> SourceReport:
    report = SourceReport(
        key="cpc_610day",
        title="CPC 6-10 Day Temperature Outlook",
        kind="image",
        credit="NOAA Climate Prediction Center",
    )
    try:
        data, media_type = fetch_image_base64(CPC_610DAY_TEMP_URL)
    except SourceError as exc:
        return report.fail(str(exc))
    report.display_url = CPC_610DAY_TEMP_URL
    report.image_base64 = data
    report.image_media_type = media_type
    return report


# The 500 mb height outlooks are the pipeline's only *anomaly* products, and the
# only ones drawn from an ensemble mean rather than a single analysis. They exist
# to answer the question a surface chart cannot: not "is a front coming?" but
# "does the ridge rebuild behind it?" Two lead times are fetched deliberately —
# a block that flattens at days 6-10 and re-amplifies at days 8-14 was dented,
# not broken, and only the pair of charts shows the difference.
#
# CPC's product pages for these (610day/500mb.php, 814day/500mb.php) are stable;
# the image filenames behind them are not, and are not guessable — the sibling
# temperature outlooks use a "610temp.new.gif" convention that these do not
# follow. So the page is read and its own <img> references are used, which is
# both how a human finds the chart and immune to a rename. Hardcoded guesses
# remain as a fallback for the day the page markup changes shape.
CPC_610DAY_500MB_PAGE = "https://www.cpc.ncep.noaa.gov/products/predictions/610day/500mb.php"
CPC_814DAY_500MB_PAGE = "https://www.cpc.ncep.noaa.gov/products/predictions/814day/500mb.php"
CPC_610DAY_500MB_CANDIDATES = (
    "https://www.cpc.ncep.noaa.gov/products/predictions/610day/500mb.gif",
    "https://www.cpc.ncep.noaa.gov/products/predictions/610day/500mb.new.gif",
    "https://www.cpc.ncep.noaa.gov/products/predictions/610day/610mb500.gif",
)
CPC_814DAY_500MB_CANDIDATES = (
    "https://www.cpc.ncep.noaa.gov/products/predictions/814day/500mb.gif",
    "https://www.cpc.ncep.noaa.gov/products/predictions/814day/500mb.new.gif",
    "https://www.cpc.ncep.noaa.gov/products/predictions/814day/814mb500.gif",
)

MAX_PAGE_CHARS = 64_000
IMG_SRC_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
CHART_SUFFIX_RE = re.compile(r"\.(?:gif|png|jpe?g)$", re.IGNORECASE)
# Site furniture that appears on every CPC page and is never the product.
PAGE_CHROME_RE = re.compile(
    r"logo|banner|butto?n|btn|spacer|arrow|header|footer|nav|icon|bullet|pixel|blank",
    re.IGNORECASE,
)
# Filenames that look like a 500 mb height product get probed first. Kept to
# distinctive substrings: a bare "mb" would match "thumbnail".
CHART_RELEVANCE_RE = re.compile(r"500|hgt|height|anom", re.IGNORECASE)


def discover_page_image_urls(page_url: str, html: str) -> list[str]:
    """Absolute URLs of plausible chart images referenced by a product page.

    Site chrome is dropped and the most product-looking filenames are ordered
    first, so the caller can probe in descending order of likelihood.
    """
    found: list[str] = []
    for src in IMG_SRC_RE.findall(html):
        url = urljoin(page_url, src.strip())
        path = urlsplit(url).path
        if not CHART_SUFFIX_RE.search(path) or PAGE_CHROME_RE.search(path):
            continue
        if url not in found:
            found.append(url)
    found.sort(key=lambda url: not CHART_RELEVANCE_RE.search(urlsplit(url).path.rsplit("/", 1)[-1]))
    return found


def _collect_page_image(
    report: SourceReport, page_url: str, fallbacks: Sequence[str]
) -> SourceReport:
    """Fetch the chart a product page points at, falling back to known URLs.

    On total failure the error names the images the page did offer, so a CI
    collect-only run diagnoses an upstream change instead of just reporting it.
    """
    discovered: list[str] = []
    page_note = ""
    try:
        discovered = discover_page_image_urls(page_url, fetch_text(page_url, MAX_PAGE_CHARS))
    except SourceError as exc:
        page_note = f"; could not read {page_url}: {exc}"
    candidates = discovered + [url for url in fallbacks if url not in discovered]
    try:
        url = resolve_first_available_image(candidates)
        data, media_type = fetch_image_base64(url)
    except SourceError:
        listed = ", ".join(discovered) if discovered else "no chart images"
        return report.fail(
            f"No usable chart image for {page_url} (page listed: {listed}){page_note}"
        )
    report.display_url = url
    report.image_base64 = data
    report.image_media_type = media_type
    return report


def collect_cpc_610day_500mb() -> SourceReport:
    return _collect_page_image(
        SourceReport(
            key="cpc_610day_500mb",
            title="CPC 6-10 Day 500 mb Height Outlook",
            kind="image",
            credit="NOAA Climate Prediction Center",
        ),
        CPC_610DAY_500MB_PAGE,
        CPC_610DAY_500MB_CANDIDATES,
    )


def collect_cpc_814day_500mb() -> SourceReport:
    return _collect_page_image(
        SourceReport(
            key="cpc_814day_500mb",
            title="CPC 8-14 Day 500 mb Height Outlook",
            kind="image",
            credit="NOAA Climate Prediction Center",
        ),
        CPC_814DAY_500MB_PAGE,
        CPC_814DAY_500MB_CANDIDATES,
    )


# ---------------------------------------------------------------------------
# Text sources
# ---------------------------------------------------------------------------

NWS_PRODUCT_LATEST = "https://api.weather.gov/products/types/{type_id}/locations/{location}/latest"
NWS_PRODUCT_LIST = "https://api.weather.gov/products/types/{type_id}/locations/{location}"


def fetch_nws_product_text(type_id: str, location: str) -> str:
    """Fetch the latest text product of a given type from api.weather.gov.

    The API splits the six-character AWIPS PIL into a 3-letter product type
    (NNN) and a 3-letter location (XXX): SWODY1 -> type SWO, location DY1,
    just like AFD/MPX. Tries the /latest endpoint first; if its response
    shape is unexpected, falls back to listing products and fetching the
    newest one.
    """
    latest_url = NWS_PRODUCT_LATEST.format(type_id=type_id, location=location)
    try:
        data = fetch_json(latest_url)
        text = data.get("productText")
        if isinstance(text, str) and text.strip():
            return text.strip()[:MAX_TEXT_CHARS]
    except SourceError:
        pass  # fall through to the list endpoint

    entries = fetch_nws_product_listing(type_id, location)
    if not entries or "@id" not in entries[0]:
        raise SourceError(f"No {type_id}/{location} products listed by api.weather.gov")
    product = fetch_json(entries[0]["@id"])
    text = product.get("productText")
    if not isinstance(text, str) or not text.strip():
        raise SourceError(f"Latest {type_id}/{location} product has no text body")
    return text.strip()[:MAX_TEXT_CHARS]


def fetch_nws_product_listing(type_id: str, location: str) -> list[dict]:
    """List a product type's recent issuances (newest first) from api.weather.gov."""
    listing = fetch_json(NWS_PRODUCT_LIST.format(type_id=type_id, location=location))
    entries = listing.get("@graph") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def collect_wpc_discussion() -> SourceReport:
    report = SourceReport(
        key="wpc_discussion",
        title="WPC Short Range Forecast Discussion",
        kind="text",
        credit="NOAA Weather Prediction Center",
    )
    try:
        report.raw_text = fetch_nws_product_text("PMD", "SPD")
    except SourceError as exc:
        return report.fail(str(exc))
    return report


def collect_spc_outlook() -> SourceReport:
    report = SourceReport(
        key="spc_outlook",
        title="SPC Day 1 Convective Outlook",
        kind="text",
        credit="NOAA Storm Prediction Center",
    )
    try:
        report.raw_text = fetch_nws_product_text("SWO", "DY1")
    except SourceError as exc:
        return report.fail(str(exc))
    return report


NHC_TWO_PAGES = {
    "Atlantic": "https://www.nhc.noaa.gov/text/MIATWOAT.shtml",
    "East Pacific": "https://www.nhc.noaa.gov/text/MIATWOEP.shtml",
}
PRE_BLOCK_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)


def collect_tropical_outlooks() -> SourceReport:
    report = SourceReport(
        key="tropics",
        title="NHC Tropical Weather Outlooks",
        kind="text",
        credit="NOAA National Hurricane Center",
    )
    sections = []
    errors = []
    for basin, url in NHC_TWO_PAGES.items():
        try:
            html = fetch_text(url, MAX_TEXT_CHARS)
        except SourceError as exc:
            errors.append(f"{basin}: {exc}")
            continue
        match = PRE_BLOCK_RE.search(html)
        if match:
            sections.append(f"--- {basin} ---\n{match.group(1).strip()}")
        else:
            errors.append(f"{basin}: no <pre> product block found at {url}")
    if not sections:
        return report.fail("; ".join(errors) or "no outlooks retrieved")
    report.raw_text = "\n\n".join(sections)[:MAX_TEXT_CHARS]
    return report


MCD_LOOKBACK_HOURS = 6
MCD_MAX_PRODUCTS = 3
NO_RECENT_MCD_TEXT = (
    f"No SPC mesoscale discussions issued in the past {MCD_LOOKBACK_HOURS} hours — "
    "no organized convective threat is being actively monitored right now."
)


def filter_recent_mcd_entries(entries: list[dict], now_utc: datetime) -> list[dict]:
    """The newest listing entries issued within the MCD lookback window."""
    cutoff = now_utc - timedelta(hours=MCD_LOOKBACK_HOURS)
    recent = []
    for entry in entries:
        issuance = entry.get("issuanceTime")
        if not isinstance(issuance, str) or "@id" not in entry:
            continue
        try:
            issued = datetime.fromisoformat(issuance.replace("Z", "+00:00"))
        except ValueError:
            continue
        if issued >= cutoff:
            recent.append(entry)
        if len(recent) == MCD_MAX_PRODUCTS:
            break
    return recent


def collect_mesoscale_discussions() -> SourceReport:
    """Active SPC mesoscale discussions, where absence is signal, not failure.

    MCDs are only issued while forecasters are actively monitoring a threat,
    so a quiet listing is reported as an explicit "nothing active" note. This
    keeps the synthesist informed either way, instead of leaving it to guess
    whether asking for MCDs is worthwhile (the old supplementary-menu model).
    """
    report = SourceReport(
        key="spc_mesoscale",
        title="SPC Mesoscale Discussions",
        kind="text",
        credit="NOAA Storm Prediction Center",
    )
    try:
        entries = fetch_nws_product_listing("SWO", "MCD")
        sections = []
        for entry in filter_recent_mcd_entries(entries, datetime.now(timezone.utc)):
            product = fetch_json(entry["@id"])
            text = product.get("productText")
            if isinstance(text, str) and text.strip():
                sections.append(text.strip())
    except SourceError as exc:
        return report.fail(str(exc))
    if sections:
        report.raw_text = "\n\n--- next mesoscale discussion ---\n\n".join(sections)[
            :MAX_TEXT_CHARS
        ]
    else:
        report.raw_text = NO_RECENT_MCD_TEXT
    return report


ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"


def collect_enso_state() -> SourceReport:
    report = SourceReport(
        key="enso",
        title="ENSO State (Oceanic Niño Index)",
        kind="text",
        credit="NOAA Climate Prediction Center",
    )
    try:
        # The ONI table is chronological from 1950 and larger than the default
        # text cap; fetch it whole so the tail is genuinely the most recent data.
        oni = fetch_text(ONI_URL, max_chars=200_000)
    except SourceError as exc:
        return report.fail(str(exc))
    lines = oni.splitlines()
    if len(lines) < 9:
        return report.fail(f"ONI table at {ONI_URL} has unexpected shape")
    # Header plus the most recent ~8 overlapping seasons is plenty of context.
    report.raw_text = "\n".join([lines[0]] + lines[-8:])
    return report


# CPC's daily teleconnection indices, in standardized units. These are
# supplementary rather than decisive — an index is a projection of the height
# field, not a substitute for it — but the *trend* in the PNA is a cheap,
# quantitative cross-check on whether an amplified western ridge is holding or
# relaxing, which nothing else in the core data provides as a number.
CPC_DAILY_INDEX_HOSTS = (
    "https://ftp.cpc.ncep.noaa.gov/cwlinks",
    "https://www.cpc.ncep.noaa.gov/products/precip/CWlink/daily_ao_index",
)
CPC_DAILY_INDEX_FILES = {
    "PNA": (
        "norm.daily.pna.index.b500101.current.ascii",
        "Pacific/North American pattern; positive favors western North American "
        "ridging with eastern troughing",
    ),
    "AO": (
        "norm.daily.ao.index.b500101.current.ascii",
        "Arctic Oscillation; negative favors high-latitude blocking",
    ),
    "NAO": (
        "norm.daily.nao.index.b500101.current.ascii",
        "North Atlantic Oscillation; influences whether an eastern cutoff can escape",
    ),
}
# These tables run daily from 1950, so the whole file has to come down for the
# tail to be the most recent data (same reason as the ONI table above).
DAILY_INDEX_MAX_CHARS = 900_000
TELECONNECTION_DAYS = 14
# Change in weekly-mean standardized units below which a trend isn't worth naming.
TELECONNECTION_TREND_THRESHOLD = 0.25
# CPC uses large sentinels for missing days; real standardized values are O(1).
DAILY_INDEX_VALUE_LIMIT = 20.0


def parse_daily_index_series(text: str) -> list[tuple[date, float]]:
    """Parse a CPC daily teleconnection table ("YYYY M D value") in order."""
    series: list[tuple[date, float]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            day = date(int(parts[0]), int(parts[1]), int(parts[2]))
            value = float(parts[3])
        except ValueError:
            continue  # header or comment line
        if abs(value) > DAILY_INDEX_VALUE_LIMIT:
            continue  # missing-data sentinel
        series.append((day, value))
    return series


def summarize_daily_index(
    name: str, note: str, series: list[tuple[date, float]], days: int = TELECONNECTION_DAYS
) -> str:
    """One block per index: latest value, week-over-week shift, and the dailies.

    The week-over-week comparison is the point — a single day's index says
    little, but a PNA falling half a standard deviation in a week is a real
    signal about an amplified ridge relaxing.
    """
    recent = series[-days:]
    if not recent:
        raise SourceError(f"{name} index table contained no usable rows")
    latest_day, latest = recent[-1]
    week = [value for _day, value in recent[-7:]]
    prior = [value for _day, value in recent[-14:-7]]
    lines = [f"{name} ({note})", f"  latest {latest_day.isoformat()}: {latest:+.2f}"]
    week_mean = sum(week) / len(week)
    if prior:
        prior_mean = sum(prior) / len(prior)
        change = week_mean - prior_mean
        if change > TELECONNECTION_TREND_THRESHOLD:
            trend = "rising"
        elif change < -TELECONNECTION_TREND_THRESHOLD:
            trend = "falling"
        else:
            trend = "little changed"
        lines.append(
            f"  7-day mean {week_mean:+.2f} vs. prior 7-day mean {prior_mean:+.2f} "
            f"({change:+.2f}, {trend})"
        )
    else:
        lines.append(f"  7-day mean {week_mean:+.2f} (no prior week available)")
    dailies = " ".join(f"{value:+.2f}" for _day, value in recent)
    lines.append(f"  daily, oldest to newest: {dailies}")
    return "\n".join(lines)


def collect_teleconnection_indices() -> SourceReport:
    """Daily PNA/AO/NAO in standardized units, with week-over-week trends.

    Each index is fetched independently: a single missing table degrades the
    source rather than failing it, since any one index still carries signal.
    """
    report = SourceReport(
        key="teleconnections",
        title="Teleconnection Indices (PNA, AO, NAO)",
        kind="text",
        credit="NOAA Climate Prediction Center",
    )
    blocks = []
    errors = []
    for name, (filename, note) in CPC_DAILY_INDEX_FILES.items():
        for host in CPC_DAILY_INDEX_HOSTS:
            try:
                text = fetch_text(f"{host}/{filename}", DAILY_INDEX_MAX_CHARS)
                blocks.append(summarize_daily_index(name, note, parse_daily_index_series(text)))
                break
            except SourceError as exc:
                errors.append(f"{name} @ {host}: {exc}")
    if not blocks:
        return report.fail("; ".join(errors) or "no teleconnection indices retrieved")
    header = (
        "Daily CPC teleconnection indices in standardized units, last "
        f"{TELECONNECTION_DAYS} days. These are observed values, not forecasts."
    )
    report.raw_text = "\n\n".join([header] + blocks)[:MAX_TEXT_CHARS]
    return report


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

COLLECTORS: list[Callable[[], SourceReport]] = [
    collect_surface_analysis,
    collect_eccc_surface_analysis,
    collect_500mb_analysis,
    collect_airmass_rgb,
    collect_airmass_fulldisk,
    collect_cpc_610day_outlook,
    collect_cpc_610day_500mb,
    collect_cpc_814day_500mb,
    collect_wpc_discussion,
    collect_spc_outlook,
    collect_mesoscale_discussions,
    collect_tropical_outlooks,
    collect_enso_state,
    collect_teleconnection_indices,
]


def collect_all() -> list[SourceReport]:
    """Run every collector, converting unexpected exceptions to failed reports."""
    reports = []
    for collector in COLLECTORS:
        try:
            report = collector()
        except Exception:  # a collector bug must not kill the run
            name = collector.__name__.removeprefix("collect_")
            report = SourceReport(
                key=name, title=name, kind="text", credit="unknown"
            ).fail(f"Collector crashed:\n{traceback.format_exc(limit=3)}")
        reports.append(report)
    return reports
