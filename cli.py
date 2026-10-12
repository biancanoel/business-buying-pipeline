"""Command-line entry point. Parses arguments and prints; the work happens in business_finder.

Run with the API key and database URL loaded from .env, for example:
    uv run --env-file .env python cli.py run
"""

import argparse
import json
from pathlib import Path

from business_finder.config import (
    ExtractionSettings,
    approximate_margin_from_environment,
    ingest_states_from_environment,
)
from business_finder.evaluation import (
    build_evaluation_set,
    format_report,
    load_evaluation_set,
    run_evaluation,
    save_evaluation_set,
)
from business_finder.extraction import make_extractor
from business_finder.extraction.business_type_judge import ClaudeBusinessTypeJudge
from business_finder.geography import Gazetteer
from business_finder.pipeline import load_saved_search_files, match_listings, run_pipeline
from business_finder.sources.bizben import BizBenAdapter
from business_finder.sources.polite_http import PoliteHttpClient
from business_finder.storage import Storage

DEFAULT_EVALUATION_DIR = Path("data/evaluation")
SAVED_SEARCHES_DIR = Path("saved_searches")


def run_command(arguments: argparse.Namespace) -> None:
    """Fetch listings, store new ones and have Claude read them."""
    http_client = PoliteHttpClient()
    storage = Storage.connect()
    try:
        summary = run_pipeline(
            sources=[BizBenAdapter(http_client, target_states=ingest_states_from_environment())],
            extractor=make_extractor(ExtractionSettings.from_environment()),
            storage=storage,
            saved_searches=load_saved_search_files(SAVED_SEARCHES_DIR),
            report=print,
            gazetteer=Gazetteer.load(),
            approximate_margin_miles=approximate_margin_from_environment(),
        )
        match_summaries = match_listings(
            storage=storage,
            judge=ClaudeBusinessTypeJudge(ExtractionSettings.from_environment()),
            saved_searches=load_saved_search_files(SAVED_SEARCHES_DIR),
            gazetteer=Gazetteer.load(),
            approximate_margin_miles=approximate_margin_from_environment(),
            report=print,
        )
    finally:
        storage.close()
        http_client.close()

    print()
    print(f"Fetched {summary.fetched} listings: {summary.new} new, {summary.seen_again} already stored.")
    print(
        f"Claude read {summary.read_by_claude}; skipped {summary.skipped_over_price_limit} over the price limit "
        f"and {summary.skipped_out_of_reach} out of reach (both are caught up if a search widens)."
    )
    if summary.failed_readings:
        print(f"{len(summary.failed_readings)} readings failed and will be retried next run:")
        for failure in summary.failed_readings:
            print(f"  {failure}")
    print_match_summaries(match_summaries)


def match_command(arguments: argparse.Namespace) -> None:
    """Match stored listings against the saved searches, without fetching anything new."""
    storage = Storage.connect()
    try:
        saved_searches = load_saved_search_files(SAVED_SEARCHES_DIR)
        for saved_search in saved_searches:
            storage.save_saved_search(saved_search)
        summaries = match_listings(
            storage=storage,
            judge=ClaudeBusinessTypeJudge(ExtractionSettings.from_environment()),
            saved_searches=saved_searches,
            gazetteer=Gazetteer.load(),
            approximate_margin_miles=approximate_margin_from_environment(),
            report=print,
        )
    finally:
        storage.close()
    print_match_summaries(summaries)


def print_match_summaries(summaries) -> None:
    for summary in summaries:
        print()
        print(
            f"{summary.search_name}: {summary.confirmed_matches} confirmed and {summary.possible_matches} possible matches "
            f"({summary.judged} business types judged this time)."
        )
        if summary.failed_judgments:
            print(f"{len(summary.failed_judgments)} judgments failed and will be retried next time:")
            for failure in summary.failed_judgments:
                print(f"  {failure}")


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

    run = commands.add_parser("run", help="Fetch listings, store new ones, have Claude read them, and match")
    run.set_defaults(handler=run_command)

    match = commands.add_parser("match", help="Match stored listings against the saved searches (no fetching)")
    match.set_defaults(handler=match_command)

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
