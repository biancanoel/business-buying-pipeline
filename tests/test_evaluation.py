"""Tests for the evaluation set and scoring. Uses fake listings and a fake extractor."""

import pytest

from business_finder.evaluation import (
    EvaluationCase,
    build_evaluation_set,
    parse_corrected_value,
    run_evaluation,
    values_match,
)
from business_finder.extraction.extractor import ExtractionError
from business_finder.models import ListingFields


class FakeExtractor:
    """Returns prepared fields for each listing text, or fails for chosen ones."""

    def __init__(self, fields_by_text: dict[str, ListingFields], failing_texts: set[str] = frozenset()) -> None:
        self.fields_by_text = fields_by_text
        self.failing_texts = failing_texts

    def extract(self, listing_text: str) -> ListingFields:
        if listing_text in self.failing_texts:
            raise ExtractionError("fake failure")
        return self.fields_by_text.get(listing_text, ListingFields())


def make_candidate(case_id: str, model_fields: dict, site_fields: dict | None = None) -> dict:
    return {
        "case_id": case_id,
        "source": "bizben",
        "source_listing_id": case_id,
        "url": f"https://example.com/{case_id}",
        "title": f"Fake listing {case_id}",
        "raw_text": f"text of {case_id}",
        "site_fields": site_fields or {},
        "model_fields": model_fields,
    }


def make_case(case_id: str, expected: dict, site_fields: ListingFields | None = None) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        source="bizben",
        source_listing_id=case_id,
        url=f"https://example.com/{case_id}",
        title=None,
        raw_text=f"text of {case_id}",
        site_fields=site_fields or ListingFields(),
        expected=expected,
    )


# --- Building the set --------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "typed", "expected"),
    [
        ("gross_revenue", "$1,500,000", 1_500_000),
        ("employee_count", "", None),
        ("requires_contractor_license", "yes", True),
        ("requires_contractor_license", "no", False),
    ],
)
def test_parse_corrected_value(field, typed, expected):
    assert parse_corrected_value(field, typed) == expected


def test_parse_corrected_value_rejects_non_numbers_for_money():
    with pytest.raises(ValueError, match="gross_revenue"):
        parse_corrected_value("gross_revenue", "about a million")


def test_build_uses_model_value_when_correct_and_correction_when_wrong():
    candidates = [make_candidate("case-01", {"asking_price": 850_000, "employee_count": None})]
    reviews = [
        {
            "case_id": "case-01",
            "fields": {
                "asking_price": {"verdict": "correct", "corrected": None},
                "employee_count": {"verdict": "wrong", "corrected": "12"},
            },
        }
    ]

    cases = build_evaluation_set(candidates, reviews)

    assert cases[0].expected == {"asking_price": 850_000, "employee_count": 12}


def test_build_leaves_out_unreviewed_listings_and_unmarked_fields():
    candidates = [make_candidate("case-01", {"asking_price": 1}), make_candidate("case-02", {"asking_price": 2})]
    reviews = [{"case_id": "case-01", "fields": {"asking_price": {"verdict": "correct"}, "city": {}}}]

    cases = build_evaluation_set(candidates, reviews)

    assert [case.case_id for case in cases] == ["case-01"]
    assert cases[0].expected == {"asking_price": 1}


# --- Scoring -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected", "actual", "matched"),
    [
        ("asking_price", 850_000, 850_000, True),
        ("asking_price", 850_000, 900_000, False),
        ("asking_price", None, 850_000, False),
        ("employee_count", 0, None, False),
        ("business_type", "residential cleaning", "maid service", None),
    ],
)
def test_values_match(field, expected, actual, matched):
    assert values_match(field, expected, actual) == matched


def test_run_scores_final_merged_values():
    site_fields = ListingFields(employee_count=0, gross_revenue=1_500_000)
    case = make_case("case-01", {"employee_count": 0, "gross_revenue": 1_500_000, "requires_contractor_license": True}, site_fields)
    extractor = FakeExtractor({"text of case-01": ListingFields(requires_contractor_license=True)})

    results = run_evaluation(extractor, [case])

    assert all(result.matched for result in results)


def test_failed_extraction_falls_back_to_site_fields():
    site_fields = ListingFields(asking_price=850_000)
    case = make_case("case-01", {"asking_price": 850_000, "requires_contractor_license": False}, site_fields)
    extractor = FakeExtractor({}, failing_texts={"text of case-01"})

    results = {result.field: result for result in run_evaluation(extractor, [case])}

    assert results["asking_price"].matched is True
    assert results["requires_contractor_license"].matched is False


