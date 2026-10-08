"""Command-line entry point. Parses arguments and prints; the work happens in business_finder.

Run with the API key loaded from .env, for example:
    uv run --env-file .env python cli.py evaluate
"""

import argparse
import json
from pathlib import Path

from business_finder.config import ExtractionSettings
from business_finder.evaluation import (
    build_evaluation_set,
    format_report,
    load_evaluation_set,
    run_evaluation,
    save_evaluation_set,
)
from business_finder.extraction import make_extractor

DEFAULT_EVALUATION_DIR = Path("data/evaluation")


def build_eval_set_command(arguments: argparse.Namespace) -> None:
    """Turn reviewed candidates into the evaluation set file."""
    candidates = json.loads(arguments.candidates.read_text())
    reviews = [json.loads(path.read_text()) for path in sorted(arguments.reviews.glob("*.json"))]
    cases = build_evaluation_set(candidates, reviews)
    save_evaluation_set(cases, arguments.output)
    checked_fields = sum(len(case.expected) for case in cases)
    print(f"Saved {len(cases)} listings with {checked_fields} checked fields to {arguments.output}")


def evaluate_command(arguments: argparse.Namespace) -> None:
    """Run the configured extractor over the evaluation set and print the report."""
    settings = ExtractionSettings.from_environment()
    cases = load_evaluation_set(arguments.eval_set)
    print(f"Evaluating {settings.provider} / {settings.model} (effort {settings.effort}) on {len(cases)} listings…\n")
    results = run_evaluation(make_extractor(settings), cases)
    print(format_report(results))


def main() -> None:
    parser = argparse.ArgumentParser(description="Business-for-Sale Finder")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-eval-set", help="Make the evaluation set from reviewed listings")
    build.add_argument("--candidates", type=Path, default=DEFAULT_EVALUATION_DIR / "candidates.json")
    build.add_argument("--reviews", type=Path, default=DEFAULT_EVALUATION_DIR / "reviews")
    build.add_argument("--output", type=Path, default=DEFAULT_EVALUATION_DIR / "eval_set.json")
    build.set_defaults(handler=build_eval_set_command)

    evaluate = commands.add_parser("evaluate", help="Measure extraction accuracy on the evaluation set")
    evaluate.add_argument("--eval-set", type=Path, default=DEFAULT_EVALUATION_DIR / "eval_set.json")
    evaluate.set_defaults(handler=evaluate_command)

    arguments = parser.parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
