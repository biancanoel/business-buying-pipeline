"""Tests for the business-type judge's provider-neutral files."""

import pytest

from business_finder.extraction.business_type_judge import (
    JudgeAnswer,
    check_answer_is_consistent,
    judge_schema_for,
    load_judge_schema,
)
from business_finder.extraction.extractor import ExtractionError
from business_finder.models import SavedSearch

SEARCH = SavedSearch(
    name="Test search",
    business_types=["physical therapy clinic", "residential cleaning", "yard care / gardening"],
    target_city="Petaluma",
    target_state="CA",
)


def test_judge_schema_file_matches_the_answer_model():
    schema = load_judge_schema()

    assert set(schema["properties"]) == set(JudgeAnswer.model_fields)
    assert set(schema["required"]) == set(JudgeAnswer.model_fields)
    assert schema["additionalProperties"] is False



def test_claude_can_only_choose_one_of_the_searchs_own_business_types():
    schema = judge_schema_for(SEARCH)

    allowed = schema["properties"]["matched_type"]["anyOf"]
    assert {"type": "string", "enum": SEARCH.business_types} in allowed
    assert {"type": "null"} in allowed


@pytest.mark.parametrize(
    ("fits", "matched_type"),
    [(True, "physical therapy clinic"), (False, None)],
    ids=["fits with a type", "does not fit, no type"],
)
def test_consistent_answers_are_accepted(fits, matched_type):
    answer = JudgeAnswer(reason="Some reason.", matched_type=matched_type, fits=fits)

    check_answer_is_consistent(answer, SEARCH)


@pytest.mark.parametrize(
    ("fits", "matched_type"),
    [(True, None), (False, "residential cleaning"), (True, "pizza restaurant")],
    ids=["fits without naming a type", "does not fit but names a type", "names a type the search does not have"],
)
def test_contradictory_answers_are_rejected(fits, matched_type):
    answer = JudgeAnswer(reason="None of the buyer's types apply.", matched_type=matched_type, fits=fits)

    with pytest.raises(ExtractionError):
        check_answer_is_consistent(answer, SEARCH)
