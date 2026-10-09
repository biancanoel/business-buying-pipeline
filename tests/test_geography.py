"""Tests for placing listings on the map and deciding whether they are within reach."""

import pytest

from business_finder.geography import (
    Gazetteer,
    GeoPoint,
    distance_miles,
    is_within_reach,
    locate,
)
from business_finder.models import ListingFields, LocationPrecision

# Real rows from the Census 2024 Gazetteer files, in their original format.
PLACES_TEXT = """USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG
CA\t0656784\t02411407\tPetaluma city\t25\tA\t37333943\t277684\t14.415\t0.107\t38.242251\t-122.628769
CA\t0656786\t02805236\tPetaluma Center CDP\t57\tS\t3038425\t0\t1.173\t0.\t38.250652\t-122.788841
CA\t0667000\t02411786\tSan Francisco city\t25\tA\t121485107\t479107241\t46.906\t184.984\t37.727239\t-123.032229
NV\t3209700\t02409993\tCarson City\t25\tA\t374694627\t7906558\t144.67\t3.053\t39.153484\t-119.743668
"""
COUNTIES_TEXT = """USPS\tGEOID\tANSICODE\tNAME\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG
CA\t06097\t01657246\tSonoma County\t4080103615\t497291856\t1575.337\t192.005\t38.525182\t-122.92611
"""

PETALUMA = GeoPoint(latitude=38.242251, longitude=-122.628769)
SAN_FRANCISCO = GeoPoint(latitude=37.727239, longitude=-123.032229)
SONOMA_COUNTY_CENTER = GeoPoint(latitude=38.525182, longitude=-122.92611)


@pytest.fixture
def gazetteer() -> Gazetteer:
    return Gazetteer.from_census_text(places_text=PLACES_TEXT, counties_text=COUNTIES_TEXT)


def test_distance_from_petaluma_to_san_francisco_is_about_forty_miles():
    assert 35 < distance_miles(PETALUMA, SAN_FRANCISCO) < 45


def test_census_names_are_found_without_their_type_words(gazetteer):
    assert gazetteer.city("Petaluma", "CA") == PETALUMA
    assert gazetteer.city("petaluma", "ca") == PETALUMA
    assert gazetteer.county("Sonoma", "CA") == SONOMA_COUNTY_CENTER
    assert gazetteer.county("Sonoma County", "CA") == SONOMA_COUNTY_CENTER
    assert gazetteer.city("Carson City", "NV") is not None


def test_a_known_city_is_an_exact_location(gazetteer):
    location = locate(ListingFields(city="Petaluma", county="Sonoma", state="CA"), gazetteer)

    assert location.precision == LocationPrecision.EXACT
    assert location.point == PETALUMA


def test_a_county_alone_is_an_approximate_location_at_its_center(gazetteer):
    location = locate(ListingFields(county="Sonoma", state="CA"), gazetteer)

    assert location.precision == LocationPrecision.APPROXIMATE
    assert location.point == SONOMA_COUNTY_CENTER


def test_a_city_the_census_does_not_know_falls_back_to_its_county(gazetteer):
    location = locate(ListingFields(city="Two Rock Ranch", county="Sonoma", state="CA"), gazetteer)

    assert location.precision == LocationPrecision.APPROXIMATE
    assert location.point == SONOMA_COUNTY_CENTER


def test_a_named_region_is_placed_at_its_best_known_city(gazetteer):
    location = locate(ListingFields(region="Bay Area", state="CA"), gazetteer)

    assert location.precision == LocationPrecision.APPROXIMATE
    assert location.point == SAN_FRANCISCO


@pytest.mark.parametrize(
    "fields",
    [
        ListingFields(region="Northern California", state="CA"),
        ListingFields(state="CA"),
        ListingFields(states=["CA", "OR"]),
        ListingFields(),
    ],
)
def test_vague_locations_are_unknown_with_no_point(gazetteer, fields):
    location = locate(fields, gazetteer)

    assert location.precision == LocationPrecision.UNKNOWN
    assert location.point is None


@pytest.mark.parametrize(
    ("precision", "miles_away", "expected"),
    [
        (LocationPrecision.EXACT, 240, True),
        (LocationPrecision.EXACT, 260, False),
        (LocationPrecision.APPROXIMATE, 290, True),
        (LocationPrecision.APPROXIMATE, 310, False),
    ],
)
def test_approximate_locations_get_a_margin_beyond_the_limit(precision, miles_away, expected):
    assert is_within_reach(precision, miles_away, max_miles=250, approximate_margin_miles=50) is expected


def test_unknown_location_is_neither_inside_nor_outside():
    assert is_within_reach(LocationPrecision.UNKNOWN, None, max_miles=250, approximate_margin_miles=50) is None
