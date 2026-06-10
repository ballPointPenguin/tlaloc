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
    args = parser.parse_args()

    from .pipeline import run

    sys.exit(run(collect_only=args.collect_only))


if __name__ == "__main__":
    main()
