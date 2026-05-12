import json
import re
import base64
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"
WPC_ANALYSIS_CHART_URL = "https://www.wpc.ncep.noaa.gov/sfc/namfnd_500_vort.gif"
WPC_SFC2_PAGE_URL = "https://www.wpc.ncep.noaa.gov/html/sfc2.shtml"
MAX_ANTHROPIC_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_WPC_HOSTS = {"www.wpc.ncep.noaa.gov", "wpc.ncep.noaa.gov"}

WPC_ANALYSIS_SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN WPC ANALYSIS CONTENT -->).*?([ \t]*<!-- END WPC ANALYSIS CONTENT -->)",
    re.DOTALL,
)

SYSTEM = """\
You generate synoptic meteorology content for the Tlaloc weather page (a static GitHub Pages site).

When asked to update the page:
1. Review the provided NOAA WPC 500 mb geopotential height and absolute vorticity analysis chart.
2. Identify the most important mid-tropospheric features: the trough/ridge pattern and its
   orientation (positively tilted, neutral, or negatively tilted), any shortwave troughs embedded
   in the flow and whether they are phasing or separating, areas of significant height falls or
   rises, and the overall character of the flow (amplified/meridional vs. zonal).
3. Call interpret_500mb_chart with a brief 2-4 sentence interpretation grounded in the chart.
4. Call update_500mb_content with the interpretation text and the provided ISO 8601 timestamp.

Keep the interpretation concise, meteorological, and cautious. Mention only features that are
clearly visible on the chart. Use plain text, not markdown.
"""

TOOLS = [
    {
        "name": "interpret_500mb_chart",
        "description": (
            "Return your brief interpretation of the provided 500 mb heights/vorticity chart. "
            "Summarize the trough/ridge structure, tilt, shortwave phasing, and height falls "
            "in 2-4 sentences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "description": "Brief plain-text interpretation of the 500 mb chart",
                },
                "generated_at": {
                    "type": "string",
                    "description": "The provided ISO 8601 timestamp to show in the page",
                },
            },
            "required": ["interpretation", "generated_at"],
        },
    },
    {
        "name": "update_500mb_content",
        "description": (
            "Replace the WPC analysis content block in index.html. "
            "The interpretation is rendered below a live NOAA WPC analysis chart image between "
            "<!-- BEGIN WPC ANALYSIS CONTENT --> and <!-- END WPC ANALYSIS CONTENT --> markers. "
            "The generated HTML reuses these CSS classes: synoptic-card, synoptic-card__image, "
            "synoptic-card__body, synoptic-card__text, synoptic-card__timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "description": "Brief plain-text interpretation to place below the chart",
                },
                "generated_at": {
                    "type": "string",
                    "description": "ISO 8601 timestamp of when the chart was interpreted",
                },
            },
            "required": ["interpretation", "generated_at"],
        },
    },
]


def format_timestamp(generated_at: str) -> str:
    dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    time_text = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%B')} {dt.day}, {dt.year} at {time_text} UTC"


def interpret_500mb_chart(interpretation: str, generated_at: str) -> dict:
    normalized = " ".join(interpretation.split())
    return {
        "success": True,
        "interpretation": normalized,
        "generated_at": generated_at,
    }


def build_500mb_html(interpretation: str, generated_at: str, chart_url: str) -> str:
    safe_interpretation = escape(" ".join(interpretation.split()))
    human_timestamp = format_timestamp(generated_at)
    return f"""<section aria-labelledby="wpc-analysis-heading">
  <h2 id="wpc-analysis-heading">WPC Analysis</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{chart_url}"
      alt="NOAA WPC North America vorticity analysis chart"
    />
    <div class="synoptic-card__body">
      <p class="synoptic-card__text">{safe_interpretation}</p>
      <p class="synoptic-card__timestamp">
        <small>Interpreted: <time datetime="{generated_at}">{human_timestamp}</time></small>
      </p>
    </div>
  </div>
</section>"""


def update_500mb_content(interpretation: str, generated_at: str, chart_url: str) -> dict:
    source = INDEX_HTML.read_text()
    html = build_500mb_html(interpretation, generated_at, chart_url)
    replacement = (
        "        <!-- BEGIN WPC ANALYSIS CONTENT -->\n"
        f"{html}\n"
        "        <!-- END WPC ANALYSIS CONTENT -->"
    )
    updated, count = WPC_ANALYSIS_SENTINEL_RE.subn(replacement, source)
    if count == 0:
        return {"success": False, "error": "WPC analysis sentinel comments not found in index.html"}
    INDEX_HTML.write_text(updated)
    return {"success": True, "path": str(INDEX_HTML), "generated_at": generated_at}


def run_tool(name: str, inputs: dict, chart_url: str) -> dict:
    if name == "interpret_500mb_chart":
        return interpret_500mb_chart(**inputs)
    if name == "update_500mb_content":
        return update_500mb_content(**inputs, chart_url=chart_url)
    raise ValueError(f"Unknown tool: {name}")


class _ImageLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for attr_name, attr_value in attrs:
            if attr_value is None:
                continue
            if attr_name.lower() in {"src", "href"}:
                self.links.append(attr_value.strip())


def is_allowed_wpc_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    host = parsed.hostname.lower() if parsed.hostname else ""
    return host in ALLOWED_WPC_HOSTS or host.endswith(".noaa.gov")


def _download_chart(url: str) -> tuple[str, str, int]:
    request = Request(url, headers={"User-Agent": "tlaloc-weather-bot/1.0"})
    with urlopen(request, timeout=30) as response:
        content_length = response.headers.get("Content-Length")
        if content_length:
            content_length_value = int(content_length)
        else:
            content_length_value = None

        if content_length_value and content_length_value > MAX_ANTHROPIC_IMAGE_BYTES:
            raise ValueError(
                f"500 mb chart size ({content_length_value} bytes) exceeds Anthropic's 5MB limit. "
                "Unable to process this image."
            )

        content_type_header = response.headers.get("Content-Type", "")
        media_type = content_type_header.split(";", 1)[0].strip().lower()
        if not media_type.startswith("image/"):
            raise ValueError(
                "Unexpected 500 mb chart content type: "
                f"parsed={media_type!r}, header={content_type_header!r}"
            )

        image_bytes = bytearray()
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            image_bytes.extend(chunk)
            if len(image_bytes) > MAX_ANTHROPIC_IMAGE_BYTES:
                raise ValueError(
                    f"Downloaded 500 mb chart size ({len(image_bytes)} bytes) exceeds "
                    "Anthropic's 5MB limit. Unable to process this image."
                )

    if not image_bytes:
        raise ValueError("500 mb chart download returned no data")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return encoded, media_type, len(image_bytes)


def discover_wpc_500mb_chart_url(page_url: str = WPC_SFC2_PAGE_URL) -> str:
    request = Request(page_url, headers={"User-Agent": "tlaloc-weather-bot/1.0"})
    with urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="ignore")

    parser = _ImageLinkExtractor()
    parser.feed(html)

    candidate_set = set()
    discovered_links = set(parser.links)
    # Some WPC pages embed image paths in inline scripts/text instead of src/href attributes.
    for match in re.finditer(
        r"""(?:https?://[^\s"'<>]+|/[^\s"'<>]+)\.(?:gif|png|jpg|jpeg)(?:\?[^\s"'<>]*)?""",
        html,
        flags=re.IGNORECASE,
    ):
        discovered_links.add(match.group(0))

    for raw_link in discovered_links:
        candidate_url = urljoin(page_url, raw_link)
        if not is_allowed_wpc_url(candidate_url):
            continue
        lower_url = candidate_url.lower()
        if "/sfc/" not in lower_url:
            continue
        if not re.search(r"\.(gif|png|jpg|jpeg)(?:\?|$)", lower_url):
            continue
        if "500" in lower_url:
            candidate_set.add(candidate_url)

    def _url_priority(url: str) -> tuple[int, int, str]:
        # Lower tuple values are higher priority.
        # Prefer WPC's canonical North America chart naming when present.
        lower_url = url.lower()
        return (
            0 if "namfnd" in lower_url else 1,
            0 if "vort" in lower_url else 1,
            lower_url,
        )

    candidates = sorted(candidate_set, key=_url_priority)

    if not candidates:
        raise RuntimeError(f"No 500 mb chart URL found on {page_url}")

    return candidates[0]


def fetch_wpc_chart(url: str) -> tuple[str, str, int, str]:
    """Download and validate the WPC analysis chart, then return it as base64 payload data.

    Args:
        url: Chart URL to download.

    Returns:
        Tuple of (base64_data, media_type, size_bytes, resolved_url).

    Raises:
        ValueError: If size/type constraints fail or content is empty.
        RuntimeError: If the chart cannot be downloaded.
    """
    try:
        encoded, media_type, size = _download_chart(url)
        return encoded, media_type, size, url
    except HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(f"Failed to download 500 mb chart from {url}: {exc}") from exc
        try:
            discovered_url = discover_wpc_500mb_chart_url()
        except RuntimeError as discovery_exc:
            raise RuntimeError(
                f"Failed to download 500 mb chart from {url} (original error: {exc}). "
                f"Fallback discovery stage failed with: {discovery_exc}"
            ) from discovery_exc
        try:
            encoded, media_type, size = _download_chart(discovered_url)
            return encoded, media_type, size, discovered_url
        except (HTTPError, URLError, OSError, ValueError) as fallback_exc:
            raise RuntimeError(
                f"Failed to download 500 mb chart from {url} (original error: {exc}). "
                f"Fallback download stage failed for {discovered_url}: {fallback_exc}"
            ) from fallback_exc
    except (URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"Failed to download 500 mb chart from {url}: {exc}") from exc


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    image_data, image_media_type, image_size, resolved_chart_url = fetch_wpc_chart(
        WPC_ANALYSIS_CHART_URL
    )
    print(f"Fetched WPC analysis chart bytes: {image_size}")
    print(f"Using WPC analysis chart URL: {resolved_chart_url}")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_media_type,
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Interpret this NOAA WPC 500 mb geopotential height and vorticity "
                        "analysis chart and update the Tlaloc page with a brief synoptic "
                        "summary. Focus on trough/ridge structure and tilt, shortwave phasing, "
                        "and significant height falls. Use this exact ISO 8601 timestamp when "
                        f"calling tools: {generated_at}"
                    ),
                },
            ],
        }
    ]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        print(f"stop_reason: {response.stop_reason}")

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"tool call: {block.name}({json.dumps(block.input, indent=2)})\n")
            result = run_tool(block.name, block.input, resolved_chart_url)
            print(f"tool result: {json.dumps(result, indent=2)}\n")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    for block in response.content:
        if block.type == "text":
            print("---")
            print(block.text)


if __name__ == "__main__":
    main()
