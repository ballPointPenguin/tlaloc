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

Stay inside what the chart can actually support:

- Describe what is plotted. Do not infer a field the chart does not show — vertical
  motion from a color, a jet axis from an airmass boundary, or an anomaly from an
  absolute value. Where such an inference is the natural reading, mark it as one
  ("the implied jet corridor", "consistent with subsidence").
- An absolute value is not a departure from normal. Unless the chart plots
  anomalies or percentiles, say "unseasonably cool/warm", never "anomalously".
- Named pattern classifications (Rex block, omega block, Rossby wave break) have
  specific geometric definitions. Use one only when the plotted field clearly meets
  it; otherwise describe the geometry you see. A descriptive phrase that is right
  beats a label that is nearly right.
"""

# What to look for in each image source.
VISION_FOCUS = {
    "surface": (
        "This is the current NOAA WPC surface analysis for North America. Identify the "
        "dominant pressure centers (highs/lows with approximate central pressures if "
        "legible), frontal boundaries and their types, regions of tight vs. slack "
        "pressure gradient, and where the surface pattern is focusing moisture or lift. "
        "Judge pressure centers on their strength, not their presence: a 1018-1020 mb "
        "summer high is unremarkable and explains little on its own, so do not credit a "
        "modest surface ridge with suppressing convection — that is the work of the "
        "upper pattern."
    ),
    "upper_500mb": (
        "This is a 500 mb geopotential height and wind analysis over the United States. "
        "Describe the longwave trough/ridge pattern and its amplitude, any cutoff lows "
        "or notable shortwaves and their tilt (positive/neutral/negative), jet streaks "
        "and their orientation, and whether the flow is progressive or blocked. "
        "Two distinctions matter here. First, whether a low is genuinely closed off "
        "from the westerlies or still open, since a closed low becomes self-retaining "
        "and pivots rather than translating downstream. Second, the actual geometry of "
        "any blocking: a Rex block is a meridionally stacked high-over-low couplet at "
        "roughly the same longitude, so if the anticyclone is not plainly poleward of "
        "the cutoff, describe what is there instead — e.g. an amplified ridge-cutoff "
        "dipole, or a quasi-stationary western ridge with a separate eastern low."
    ),
    "airmass": (
        "This is a GOES East CONUS Air Mass RGB satellite image. Color guide: "
        "reds/oranges = dry, ozone-rich, high-PV air of stratospheric origin; greens = "
        "tropical moist air; dark blues = cold dry upper troposphere; white = high cold "
        "cloud tops. Identify dry high-PV intrusions and streamers, the jet corridor "
        "implied by the sharpest airmass boundaries, and organized deep convection. "
        "Describe where a high-PV feature runs and which flank of a trough or cutoff it "
        "wraps around — high-PV air is most closely associated with the cyclonic side of "
        "the upper jet and the wrapping trough circulation. The RGB shows a dry, "
        "ozone-rich signature; it does not by itself establish literal descent, so say "
        "the air is dry and high-PV rather than asserting it is descending. Likewise "
        "call a sharp boundary the likely jet corridor, not a precisely located jet axis, "
        "since no isotachs are plotted here."
    ),
    "airmass_fd": (
        "This is a GOES East Full Disk Air Mass RGB satellite image spanning the "
        "Americas. Color guide: reds/oranges = dry, ozone-rich, high-PV air of "
        "stratospheric origin; greens = tropical moist air; dark blues = cold dry upper "
        "troposphere; white = high cold cloud tops. A CONUS-focused view is analyzed "
        "separately, so concentrate on what this frame adds: the polar jet and airmass "
        "boundaries across Canada and the Arctic, and tropical waves or organized "
        "convection over Mexico, Central America, the Caribbean, and the eastern "
        "Pacific and Atlantic basins."
    ),
    "eccc_surface": (
        "This is the Canadian Meteorological Centre (Environment and Climate Change "
        "Canada) surface analysis for North America. Focus on Canada and the "
        "cross-border pattern: pressure centers and frontal boundaries over Canadian "
        "provinces and territories, how they connect to systems over the United "
        "States, and regions of tight pressure gradient or focused moisture and lift "
        "north of the border."
    ),
    "cpc_610day": (
        "This is the NOAA Climate Prediction Center 6-10 day temperature outlook, "
        "showing probabilities of above- or below-normal temperatures. Describe where "
        "warm and cool anomalies are favored, how strong the probabilities are, and "
        "what the anomaly pattern implies about whether the current upper-level "
        "regime persists or breaks down beyond the short range."
    ),
    "cpc_610day_500mb": (
        "This is the NOAA Climate Prediction Center 6-10 day 500 mb height outlook: an "
        "ensemble-mean forecast of mid-tropospheric heights and their anomalies. Give "
        "the location, sign, and strength of the main height anomaly centers, and the "
        "position of the ridge axis and any trough axes. Say what the field implies "
        "about amplification: whether positive anomalies over western North America are "
        "holding, shrinking, or retreating in latitude and longitude, how far into "
        "western North America the zero-anomaly line reaches, and whether the flow is "
        "becoming more zonal. This is an anomaly product, so departures from normal can "
        "be stated directly here."
    ),
    "cpc_814day_500mb": (
        "This is the NOAA Climate Prediction Center 8-14 day 500 mb height outlook: an "
        "ensemble-mean forecast of mid-tropospheric heights and their anomalies at a "
        "longer lead than the 6-10 day chart. Give the location, sign, and strength of "
        "the main height anomaly centers and the ridge and trough axes. Pay particular "
        "attention to whether a positive height anomaly is present over western Canada, "
        "the northern Rockies, and the northern Plains, and to where the ridge core "
        "sits — a ridge that has retreated toward the Southwest and northern Mexico is a "
        "different outcome from one still centered over the northern Rockies. This is an "
        "anomaly product, so departures from normal can be stated directly here."
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
