import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"
COD_500MB_URL_TEMPLATE = "https://weather.cod.edu/wxdata/upper/US/500/US500.{date}.{hour}.gif"
SYNOPTIC_LOOKBACK = 4

WPC_ANALYSIS_SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN WPC ANALYSIS CONTENT -->).*?([ \t]*<!-- END WPC ANALYSIS CONTENT -->)",
    re.DOTALL,
)

SYSTEM = """\
You generate synoptic meteorology content for the Tlaloc weather page (a static GitHub Pages site).

When asked to update the page:
1. Review the provided College of DuPage 500 mb geopotential height and wind analysis chart.
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
            "Return your brief interpretation of the provided 500 mb heights/wind chart. "
            "Summarize the trough/ridge structure and tilt, shortwave phasing, and jet "
            "stream orientation in 2-4 sentences."
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
            "Replace the 500mb analysis content block in index.html. "
            "The interpretation is rendered below a College of DuPage 500 mb analysis chart "
            "between <!-- BEGIN WPC ANALYSIS CONTENT --> and <!-- END WPC ANALYSIS CONTENT --> markers. "
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
  <h2 id="wpc-analysis-heading">500mb Analysis</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{chart_url}"
      alt="College of DuPage 500 mb geopotential height and wind analysis chart"
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


def resolve_500mb_chart_url(now_utc: datetime | None = None) -> str:
    probe_time = now_utc or datetime.now(timezone.utc)
    hour = 12 if probe_time.hour >= 12 else 0
    aligned = probe_time.replace(hour=hour, minute=0, second=0, microsecond=0)

    for step in range(SYNOPTIC_LOOKBACK):
        t = aligned - timedelta(hours=12 * step)
        url = COD_500MB_URL_TEMPLATE.format(date=t.strftime("%Y%m%d"), hour=t.strftime("%H"))
        try:
            request = Request(url, headers={"User-Agent": "tlaloc-weather-bot/1.0"})
            with urlopen(request, timeout=10) as response:
                if response.status == 200:
                    return url
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise RuntimeError(f"Error probing {url}: {exc}") from exc
        except (URLError, OSError) as exc:
            raise RuntimeError(f"Error probing {url}: {exc}") from exc

    raise RuntimeError(
        f"No COD 500mb chart found; tried {SYNOPTIC_LOOKBACK} synoptic times back from "
        f"{probe_time.strftime('%Y-%m-%dT%H:00Z')}"
    )


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    chart_url = resolve_500mb_chart_url()
    print(f"Using COD 500mb chart URL: {chart_url}")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": chart_url,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Interpret this 500 mb geopotential height and wind analysis chart and "
                        "update the Tlaloc page with a brief synoptic summary. Focus on trough/ridge "
                        "structure and tilt, shortwave phasing, and jet stream orientation. "
                        f"Use this exact ISO 8601 timestamp when calling tools: {generated_at}"
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
            result = run_tool(block.name, block.input, chart_url)
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
