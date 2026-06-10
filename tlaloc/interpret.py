"""Per-source interpretation: one independent Claude call per source.

Image sources get a vision call with a source-specific analytical focus.
Text sources get a cheap distillation call that compresses a long official
product into a few sentences. The outputs are the building blocks the
synthesis stage works from.
"""

import anthropic

from .config import TEXT_MODEL, VISION_MODEL
from .sources import SourceReport

VISION_SYSTEM = """\
You are an operational meteorologist writing brief chart interpretations for an
educated audience of weather enthusiasts. Be concrete and cautious: describe only
features clearly visible in the image, name regions precisely, and avoid hedging
boilerplate. Plain text only, no markdown, 3-5 sentences.
"""

# What to look for in each image source.
VISION_FOCUS = {
    "surface": (
        "This is the current NOAA WPC surface analysis for North America. Identify the "
        "dominant pressure centers (highs/lows with approximate central pressures if "
        "legible), frontal boundaries and their types, regions of tight vs. slack "
        "pressure gradient, and where the surface pattern is focusing moisture or lift."
    ),
    "upper_500mb": (
        "This is a 500 mb geopotential height and wind analysis over the United States. "
        "Describe the longwave trough/ridge pattern and its amplitude, any cutoff lows "
        "or notable shortwaves and their tilt (positive/neutral/negative), jet streaks "
        "and their orientation, and whether the flow is progressive or blocked."
    ),
    "airmass": (
        "This is a GOES East CONUS Air Mass RGB satellite image. Color guide: "
        "reds/oranges = dry stratospheric intrusions and high-PV air; greens = tropical "
        "moist air; dark blues = cold dry upper troposphere; white = high cold cloud "
        "tops. Identify stratospheric dry intrusions, PV streamers or cutoffs, the jet "
        "axis implied by airmass boundaries, and organized deep convection."
    ),
}

TEXT_SYSTEM = """\
You distill official weather and climate products — forecast discussions, outlooks,
or data tables — for a meteorological briefing. For discussions and outlooks, extract
the synoptically significant content: the systems and hazards being discussed, where
the forecaster's attention is focused, and any notable uncertainty. For data tables,
state the current value and recent trend plainly (e.g. for an ONI table, the current
ENSO phase and which way it is drifting). Drop boilerplate, headers, and
administrative text, and don't comment on the product's format — just brief its
content. Plain text only, no markdown, at most 120 words.
"""


def interpret_image(client: anthropic.Anthropic, report: SourceReport) -> str:
    focus = VISION_FOCUS.get(report.key, "Interpret the most important synoptic features.")
    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=1024,
        system=VISION_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": report.image_media_type,
                            "data": report.image_base64,
                        },
                    },
                    {"type": "text", "text": focus},
                ],
            }
        ],
    )
    return _text_of(response)


def summarize_text(client: anthropic.Anthropic, report: SourceReport) -> str:
    response = client.messages.create(
        model=TEXT_MODEL,
        max_tokens=512,
        system=TEXT_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Product: {report.title} ({report.credit}).\n\n"
                    f"{report.raw_text}\n\n"
                    "Distill this product per your instructions."
                ),
            }
        ],
    )
    return _text_of(response)


def interpret_all(client: anthropic.Anthropic, reports: list[SourceReport]) -> None:
    """Fill in report.summary for every successful source.

    An interpretation failure (API error, etc.) downgrades that source to
    failed rather than aborting the run.
    """
    for report in reports:
        if report.status != "ok":
            continue
        try:
            if report.kind == "image":
                report.summary = interpret_image(client, report)
            else:
                report.summary = summarize_text(client, report)
        except (anthropic.APIError, RuntimeError) as exc:
            report.fail(f"Interpretation failed: {exc}")


def _text_of(response) -> str:
    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise RuntimeError(f"Model returned no text (stop_reason={response.stop_reason})")
    return text
