"""Measure extraction accuracy against hand-checked listings.

The evaluation set is a list of real listings, each with the values a
person confirmed by reading it. Running the evaluation extracts each
listing again, merges the result with the site's own fields (exactly as
the pipeline stores it), and compares every checked field.

Scoring the final stored values, not the model's answer alone, measures
what matters to the buyer. It also means a site field the model never sees
(such as BizBen's "0 employees") still counts when it is right.

Business type is free text, so it is not scored automatically. The report
lists every listing where the new description differs from the checked one,
for a person to judge.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from business_finder.extraction.extractor import ExtractionError, Extractor, merge_fields
from business_finder.models import ListingFields

MONEY_AND_COUNT_FIELDS = {
    "asking_price",
    "gross_revenue",
    "seller_discretionary_earnings",
    "down_payment",
    "year_established",
    "employee_count",
}
YES_NO_FIELDS = {"requires_contractor_license"}
TEXT_FIELDS = {"business_name", "business_type", "city", "county", "state"}
FIELDS_SCORED_BY_A_PERSON = {"business_type"}

# A founding year worked out from "in business N years" depends on the year
# the evaluation runs, so it may be one off from the year when it was checked.
YEAR_TOLERANCE = 1


class EvaluationCase(BaseModel):
    """One hand-checked listing.

    `expected` holds only the fields a person checked; a value of None
    means "the listing does not state this".
    """

    case_id: str
    source: str
    source_listing_id: str
    url: str
    title: str | None
    raw_text: str
    site_fields: ListingFields
    expected: dict[str, str | int | bool | None]


@dataclass
class FieldResult:
    """How one field of one listing came out in an evaluation run."""

    case_id: str
    field: str
    expected: object
    actual: object
    matched: bool | None  # None for fields a person has to judge


# --- Building the evaluation set from review marks -------------------------


def parse_corrected_value(field: str, corrected: str | None) -> str | int | bool | None:
    """Turn what the reviewer typed into the value the field should hold.

    An empty box means "not stated". Money and counts accept "$1,250,000"
    as well as "1250000". Yes/no fields accept "yes" and "no".
    """
    if corrected is None or corrected.strip() == "":
        return None
    text = corrected.strip()
    if field in MONEY_AND_COUNT_FIELDS:
        digits = text.replace("$", "").replace(",", "").strip()
        if not digits.isdigit():
            raise ValueError(f"{field}: expected a whole number, got {corrected!r}")
        return int(digits)
    if field in YES_NO_FIELDS:
        if text.lower() not in {"yes", "no"}:
            raise ValueError(f"{field}: expected yes or no, got {corrected!r}")
        return text.lower() == "yes"
    return text


def expected_values_from_review(review: dict, model_fields: dict) -> dict:
    """Combine a reviewer's marks with the model's answers into checked values.

    A field marked correct keeps the model's value; a field marked wrong
    takes the reviewer's correction. Unmarked fields are left out.
    """
    expected = {}
    for field, mark in review.get("fields", {}).items():
        verdict = mark.get("verdict")
        if verdict == "correct":
            expected[field] = model_fields.get(field)
        elif verdict == "wrong":
            expected[field] = parse_corrected_value(field, mark.get("corrected"))
    return expected


def build_evaluation_set(candidates: list[dict], reviews: list[dict]) -> list[EvaluationCase]:
    """Make evaluation cases from the reviewed candidates.

    Candidates nobody reviewed are left out.
    """
    reviews_by_case = {review["case_id"]: review for review in reviews}
    cases = []
    for candidate in candidates:
        review = reviews_by_case.get(candidate["case_id"])
        if review is None:
            continue
        expected = expected_values_from_review(review, candidate.get("model_fields") or {})
        if not expected:
            continue
        cases.append(
            EvaluationCase(
                case_id=candidate["case_id"],
                source=candidate["source"],
                source_listing_id=candidate["source_listing_id"],
                url=candidate["url"],
                title=candidate.get("title"),
                raw_text=candidate["raw_text"],
                site_fields=ListingFields(**candidate["site_fields"]),
                expected=expected,
            )
        )
    return cases


def save_evaluation_set(cases: list[EvaluationCase], path: Path) -> None:
    """Write the evaluation set as readable JSON."""
    path.write_text(json.dumps([case.model_dump(mode="json") for case in cases], indent=2))


def load_evaluation_set(path: Path) -> list[EvaluationCase]:
    """Read an evaluation set written by `save_evaluation_set`."""
    return [EvaluationCase(**case) for case in json.loads(path.read_text())]


# --- Running and scoring -----------------------------------------------------


def values_match(field: str, expected: object, actual: object) -> bool | None:
    """Decide whether an extracted value matches the checked one.

    Returns None for fields a person has to judge (business type).
    """
    if field in FIELDS_SCORED_BY_A_PERSON:
        return None
    if expected is None or actual is None:
        return expected is None and actual is None
    if field == "year_established":
        return abs(int(expected) - int(actual)) <= YEAR_TOLERANCE
    if field in TEXT_FIELDS:
        return str(expected).strip().casefold() == str(actual).strip().casefold()
    return expected == actual


def final_fields_for_case(extractor: Extractor, case: EvaluationCase) -> ListingFields:
    """Extract one listing and merge it with its site fields, as the pipeline does.

    If extraction fails for this listing, only the site fields are used,
    which is also what the pipeline would store.
    """
    try:
        model_fields = extractor.extract(case.raw_text)
    except ExtractionError:
        model_fields = ListingFields()
    return merge_fields(case.site_fields, model_fields)


def run_evaluation(extractor: Extractor, cases: list[EvaluationCase]) -> list[FieldResult]:
    """Extract every case and compare each checked field."""
    results = []
    for case in cases:
        final_fields = final_fields_for_case(extractor, case)
        for field, expected in case.expected.items():
            actual = getattr(final_fields, field)
            results.append(
                FieldResult(
                    case_id=case.case_id,
                    field=field,
                    expected=expected,
                    actual=actual,
                    matched=values_match(field, expected, actual),
                )
            )
    return results


def accuracy_by_field(results: list[FieldResult]) -> dict[str, tuple[int, int]]:
    """Return (matched, checked) per automatically scored field."""
    totals: dict[str, tuple[int, int]] = {}
    for result in results:
        if result.matched is None:
            continue
        matched, checked = totals.get(result.field, (0, 0))
        totals[result.field] = (matched + int(result.matched), checked + 1)
    return dict(sorted(totals.items()))


def format_report(results: list[FieldResult]) -> str:
    """Return a plain-text report: accuracy per field, then every miss."""
    totals = accuracy_by_field(results)
    overall_matched = sum(matched for matched, _ in totals.values())
    overall_checked = sum(checked for _, checked in totals.values())

    lines = ["Extraction accuracy (final stored values vs hand-checked)", ""]
    for field, (matched, checked) in totals.items():
        lines.append(f"  {field:32} {matched:3d} / {checked:<3d} {matched / checked:6.0%}")
    if overall_checked:
        lines.append(f"  {'all scored fields':32} {overall_matched:3d} / {overall_checked:<3d} {overall_matched / overall_checked:6.0%}")

    misses = [result for result in results if result.matched is False]
    lines += ["", f"Misses ({len(misses)}):"]
    for result in misses:
        lines.append(f"  {result.case_id} {result.field}: expected {result.expected!r}, got {result.actual!r}")

    to_judge = [
        result
        for result in results
        if result.matched is None and str(result.expected).casefold() != str(result.actual).casefold()
    ]
    lines += ["", f"Business types that changed, for a person to judge ({len(to_judge)}):"]
    for result in to_judge:
        lines.append(f"  {result.case_id}: checked {result.expected!r}, now {result.actual!r}")
    return "\n".join(lines)
