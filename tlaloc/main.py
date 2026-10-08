import argparse
import sys

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="tlaloc",
        description="Daily synoptic pattern analysis for North America",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Fetch and report on all data sources without calling Claude or writing the page",
    )
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const=".dry-run",
        metavar="DIR",
        help=(
            "Run the full pipeline (including Claude calls) but write index.html, data/ and "
            "archive/ under DIR (default .dry-run/) instead of the repo, leaving the "
            "production page and history untouched"
        ),
    )
    args = parser.parse_args()

    from .pipeline import run

    sys.exit(run(collect_only=args.collect_only, dry_run_dir=args.dry_run))


if __name__ == "__main__":
    main()
