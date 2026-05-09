import base64
import json
import re
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"
SURFACE_ANALYSIS_URL = "https://www.wpc.ncep.noaa.gov/sfc/namussfcwbg.gif"

SYNOPTIC_SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN SYNOPTIC CONTENT -->).*?([ \t]*<!-- END SYNOPTIC CONTENT -->)",
    re.DOTALL,
)

SYSTEM = """\
You generate synoptic meteorology content for the Tlaloc weather page (a static GitHub Pages site).

When asked to update the page:
1. Review the provided NOAA WPC current surface analysis chart image.
2. Identify the most important visible synoptic-scale features: isobars and pressure gradient,
   frontal boundaries (cold, warm, occluded, stationary), pressure centers (H/L), and the
   broad flow pattern around those systems.
3. Call interpret_synoptic_chart with a brief 2-4 sentence interpretation grounded in the chart.
4. Call update_synoptic_content with the interpretation text and the provided ISO 8601 timestamp.

Keep the interpretation concise, meteorological, and cautious. Mention only features that are
clearly visible on the chart. Use plain text, not markdown.
"""

TOOLS = [
    {
        "name": "interpret_synoptic_chart",
        "description": (
            "Return your brief synoptic interpretation of the provided surface analysis chart. "
            "Summarize the most important visible isobars, fronts, pressure centers, and broad "
            "flow patterns in 2-4 sentences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "description": "Brief plain-text synoptic interpretation of the chart",
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
        "name": "update_synoptic_content",
        "description": (
            "Replace the synoptic content block in index.html. "
            "The interpretation is rendered below a live NOAA chart image between "
            "<!-- BEGIN SYNOPTIC CONTENT --> and <!-- END SYNOPTIC CONTENT --> markers. "
            "The generated HTML uses these CSS classes: synoptic-card, synoptic-card__image, "
            "synoptic-card__body, synoptic-card__text, synoptic-card__timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "description": "Brief plain-text synoptic interpretation to place below the chart",
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


def fetch_surface_analysis_chart() -> bytes:
    with urllib.request.urlopen(SURFACE_ANALYSIS_URL) as resp:
        return resp.read()


def format_timestamp(generated_at: str) -> str:
    dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    time_text = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%B')} {dt.day}, {dt.year} at {time_text} UTC"


def interpret_synoptic_chart(interpretation: str, generated_at: str) -> dict:
    normalized = " ".join(interpretation.split())
    return {
        "success": True,
        "interpretation": normalized,
        "generated_at": generated_at,
    }


def build_synoptic_html(interpretation: str, generated_at: str) -> str:
    safe_interpretation = escape(" ".join(interpretation.split()))
    human_timestamp = format_timestamp(generated_at)
    return f"""<section aria-labelledby="synoptic-heading">
  <h2 id="synoptic-heading">Surface Analysis</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{SURFACE_ANALYSIS_URL}"
      alt="NOAA WPC current surface analysis chart for North America"
    />
    <div class="synoptic-card__body">
      <p class="synoptic-card__text">{safe_interpretation}</p>
      <p class="synoptic-card__timestamp">
        <small>Interpreted: <time datetime="{generated_at}">{human_timestamp}</time></small>
      </p>
    </div>
  </div>
</section>"""


def update_synoptic_content(interpretation: str, generated_at: str) -> dict:
    source = INDEX_HTML.read_text()
    html = build_synoptic_html(interpretation, generated_at)
    replacement = (
        "        <!-- BEGIN SYNOPTIC CONTENT -->\n"
        f"{html}\n"
        "        <!-- END SYNOPTIC CONTENT -->"
    )
    updated, count = SYNOPTIC_SENTINEL_RE.subn(replacement, source)
    if count == 0:
        return {"success": False, "error": "Synoptic sentinel comments not found in index.html"}
    INDEX_HTML.write_text(updated)
    return {"success": True, "path": str(INDEX_HTML), "generated_at": generated_at}


def run_tool(name: str, inputs: dict) -> dict:
    if name == "interpret_synoptic_chart":
        return interpret_synoptic_chart(**inputs)
    if name == "update_synoptic_content":
        return update_synoptic_content(**inputs)
    raise ValueError(f"Unknown tool: {name}")


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    image_bytes = fetch_surface_analysis_chart()
    image_data = base64.b64encode(image_bytes).decode("utf-8")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/gif",
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Interpret this NOAA WPC current surface analysis chart and update the "
                        "Tlaloc page with a brief synoptic summary. Use this exact ISO 8601 "
                        f"timestamp when calling tools: {generated_at}"
                    ),
                },
            ],
        }
    ]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-5",
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
            result = run_tool(block.name, block.input)
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
