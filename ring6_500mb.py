import json
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"
HEIGHTS_500MB_URL = "https://www.wpc.ncep.noaa.gov/500/500z_ana.gif"

HEIGHTS_500MB_SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN 500MB CONTENT -->).*?([ \t]*<!-- END 500MB CONTENT -->)",
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
            "Replace the 500 mb content block in index.html. "
            "The interpretation is rendered below a live NOAA WPC 500 mb chart image between "
            "<!-- BEGIN 500MB CONTENT --> and <!-- END 500MB CONTENT --> markers. "
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


def build_500mb_html(interpretation: str, generated_at: str) -> str:
    safe_interpretation = escape(" ".join(interpretation.split()))
    human_timestamp = format_timestamp(generated_at)
    return f"""<section aria-labelledby="heights-500mb-heading">
  <h2 id="heights-500mb-heading">500 mb Heights / Vorticity</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{HEIGHTS_500MB_URL}"
      alt="NOAA WPC 500 mb geopotential height and absolute vorticity analysis for North America"
    />
    <div class="synoptic-card__body">
      <p class="synoptic-card__text">{safe_interpretation}</p>
      <p class="synoptic-card__timestamp">
        <small>Interpreted: <time datetime="{generated_at}">{human_timestamp}</time></small>
      </p>
    </div>
  </div>
</section>"""


def update_500mb_content(interpretation: str, generated_at: str) -> dict:
    source = INDEX_HTML.read_text()
    html = build_500mb_html(interpretation, generated_at)
    replacement = (
        "        <!-- BEGIN 500MB CONTENT -->\n"
        f"{html}\n"
        "        <!-- END 500MB CONTENT -->"
    )
    updated, count = HEIGHTS_500MB_SENTINEL_RE.subn(replacement, source)
    if count == 0:
        return {"success": False, "error": "500 mb sentinel comments not found in index.html"}
    INDEX_HTML.write_text(updated)
    return {"success": True, "path": str(INDEX_HTML), "generated_at": generated_at}


def run_tool(name: str, inputs: dict) -> dict:
    if name == "interpret_500mb_chart":
        return interpret_500mb_chart(**inputs)
    if name == "update_500mb_content":
        return update_500mb_content(**inputs)
    raise ValueError(f"Unknown tool: {name}")


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": HEIGHTS_500MB_URL,
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
