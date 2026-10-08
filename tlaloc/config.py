"""Central configuration for the Tlaloc pipeline.

Model choices and limits can be overridden with environment variables so the
GitHub Actions workflow (or local experiments) can tune cost/quality without
code changes.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"
# Per-day JSON records (the durable history) and the derived archive pages.
DATA_DIR = REPO_ROOT / "data"
ARCHIVE_DIR = REPO_ROOT / "archive"

# Vision interpretation of individual charts/imagery.
VISION_MODEL = os.environ.get("TLALOC_VISION_MODEL", "claude-sonnet-5-5")
# Distillation of long text products (forecast discussions, outlooks).
TEXT_MODEL = os.environ.get("TLALOC_TEXT_MODEL", "claude-haiku-5-5")
# Haiku 5.5 thinks adaptively by default; "medium" is the guide's recommendation for
# plain text tasks (thinking off or "low" tends to leak reasoning into replies).
TEXT_EFFORT = os.environ.get("TLALOC_TEXT_EFFORT", "medium")
# The final meta-synthesis across all source summaries.
SYNTHESIS_MODEL = os.environ.get("TLALOC_SYNTHESIS_MODEL", "claude-opus-5-5")

# Thinking depth for the synthesis call. Opus 5.5 defaults to "medium"; "high" is
# worth the (small, once-daily) cost for the diagnostic-discipline rules.
SYNTHESIS_EFFORT = os.environ.get("TLALOC_SYNTHESIS_EFFORT", "high")

USER_AGENT = "tlaloc-weather-bot/2.0 (+https://github.com/ballPointPenguin/tlaloc)"
HTTP_TIMEOUT_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 10

# Anthropic's hard limit for a single image is 5 MB.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Cap on raw text-product characters fed to a model, so an unexpectedly huge
# response from an external API can't blow up a request.
MAX_TEXT_CHARS = 16_000

# Minimum successful sources required before we synthesize and publish.
# Below these thresholds the run aborts and yesterday's page stays in place.
MIN_IMAGE_SOURCES = 1
MIN_TOTAL_SOURCES = 2
