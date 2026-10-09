"""Placing listings on the map, and deciding whether they are within reach.

Listings state their location at different levels: a city, a county, a
named region ("Bay Area") or just a state. Each is turned into a point
with a precision tag:

- exact: a city the Census knows, at the city's center.
- approximate: a county (at its center) or a named region (at its
  best-known city, e.g. San Francisco for the Bay Area).
- unknown: only a state, several states, or a region too broad to place
  ("Northern California"). These listings are kept and surfaced, never
  dropped.

Coordinates come from the US Census Bureau's 2024 Gazetteer files (see
data/README.md), so lookups work offline with no API.
"""

import math
from dataclasses import dataclass
from importlib import resources

from business_finder.models import ListingFields, LocationPrecision

DATA_PACKAGE = "business_finder.data"
PLACES_FILE = "census_2024_place_ca_or_nv.txt"
COUNTIES_FILE = "census_2024_counties_ca_or_nv.txt"

EARTH_RADIUS_MILES = 3958.8

# Words the Census adds to place names ("Petaluma city", "Sonoma County").
# Consolidated cities such as "Carson City" have none and are kept as is.
CENSUS_NAME_ENDINGS = (" city", " town", " CDP", " County")

# Named regions and the best-known city used as their center: (city, state).
# Regions not listed here, and ones too broad to place ("Northern
# California"), have an unknown location.
REGION_CENTER_CITIES = {
    "bay area": ("San Francisco", "CA"),
    "greater bay area": ("San Francisco", "CA"),
    "sf bay area": ("San Francisco", "CA"),
    "san francisco bay area": ("San Francisco", "CA"),
    "east bay": ("Oakland", "CA"),
    "east bay area": ("Oakland", "CA"),
    "north bay": ("Santa Rosa", "CA"),
    "south bay": ("San Jose", "CA"),
    "silicon valley": ("San Jose", "CA"),
    "peninsula": ("San Mateo", "CA"),
    "central valley": ("Fresno", "CA"),
    "sacramento valley": ("Sacramento", "CA"),
    "greater sacramento": ("Sacramento", "CA"),
    "northern nevada": ("Reno", "NV"),
    "southern nevada": ("Las Vegas", "NV"),
    "central oregon": ("Bend", "OR"),
    "southern california": ("Los Angeles", "CA"),
    "socal": ("Los Angeles", "CA"),
}


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Location:
    """Where a listing is, as well as we can tell."""

    point: GeoPoint | None
    precision: LocationPrecision


UNKNOWN_LOCATION = Location(point=None, precision=LocationPrecision.UNKNOWN)


def distance_miles(first: GeoPoint, second: GeoPoint) -> float:
    """Straight-line distance between two points, in miles (haversine formula)."""
    latitude_1, latitude_2 = math.radians(first.latitude), math.radians(second.latitude)
    latitude_change = latitude_2 - latitude_1
    longitude_change = math.radians(second.longitude - first.longitude)
    a = (
        math.sin(latitude_change / 2) ** 2
        + math.cos(latitude_1) * math.cos(latitude_2) * math.sin(longitude_change / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def normalize_name(name: str) -> str:
    """Make a place name comparable: no Census type word, lowercase, single spaces."""
    name = " ".join(name.split())
    for ending in CENSUS_NAME_ENDINGS:
        if name.endswith(ending):
            name = name.removesuffix(ending)
            break
    return name.casefold()


def read_census_rows(text: str) -> list[dict[str, str]]:
    """Read a Census Gazetteer file: tab-separated, with a header row."""
    lines = [line for line in text.splitlines() if line.strip()]
    header = [column.strip() for column in lines[0].split("\t")]
    return [dict(zip(header, (value.strip() for value in line.split("\t")))) for line in lines[1:]]


class Gazetteer:
    """Looks up the center point of a city or county by name and state."""

    def __init__(self, cities: dict[tuple[str, str], GeoPoint], counties: dict[tuple[str, str], GeoPoint]) -> None:
        self.cities = cities
        self.counties = counties

    @classmethod
    def from_census_text(cls, places_text: str, counties_text: str) -> "Gazetteer":
        """Build lookups from the text of the Census places and counties files."""
        cities: dict[tuple[str, str], GeoPoint] = {}
        incorporated_keys: set[tuple[str, str]] = set()
        for row in read_census_rows(places_text):
            key = (normalize_name(row["NAME"]), row["USPS"].upper())
            # A city or town wins over an unincorporated place (CDP) with the same name.
            is_incorporated = not row["NAME"].endswith(" CDP")
            if key in incorporated_keys and not is_incorporated:
                continue
            cities[key] = point_from_row(row)
            if is_incorporated:
                incorporated_keys.add(key)

        counties = {
            (normalize_name(row["NAME"]), row["USPS"].upper()): point_from_row(row)
            for row in read_census_rows(counties_text)
        }
        return cls(cities, counties)

    @classmethod
    def load(cls) -> "Gazetteer":
        """Build lookups from the Census files packaged with this project."""
        data = resources.files(DATA_PACKAGE)
        return cls.from_census_text(
            places_text=data.joinpath(PLACES_FILE).read_text(),
            counties_text=data.joinpath(COUNTIES_FILE).read_text(),
        )

    def city(self, name: str, state: str) -> GeoPoint | None:
        return self.cities.get((normalize_name(name), state.upper()))

    def county(self, name: str, state: str) -> GeoPoint | None:
        return self.counties.get((normalize_name(name), state.upper()))


def point_from_row(row: dict[str, str]) -> GeoPoint:
    return GeoPoint(latitude=float(row["INTPTLAT"]), longitude=float(row["INTPTLONG"]))


def locate(fields: ListingFields, gazetteer: Gazetteer) -> Location:
    """Place a listing as precisely as its stated location allows.

    Tries the city, then the county, then a named region. A city or county
    needs a single state to look up, because the same names exist in
    different states.
    """
    state = fields.state
    if state and fields.city:
        point = gazetteer.city(fields.city, state)
        if point:
            return Location(point=point, precision=LocationPrecision.EXACT)
    if state and fields.county:
        point = gazetteer.county(fields.county, state)
        if point:
            return Location(point=point, precision=LocationPrecision.APPROXIMATE)
    if fields.region:
        center = REGION_CENTER_CITIES.get(" ".join(fields.region.split()).casefold())
        if center:
            point = gazetteer.city(*center)
            if point:
                return Location(point=point, precision=LocationPrecision.APPROXIMATE)
    return UNKNOWN_LOCATION


def is_within_reach(
    precision: LocationPrecision,
    miles_away: float | None,
    max_miles: float,
    approximate_margin_miles: float,
) -> bool | None:
    """Decide whether a listing is close enough to the target city.

    An approximate location is a county's or region's center, and the
    business may be anywhere in it, so it gets extra room beyond the limit.
    Returns None when the location is unknown: such listings are neither
    inside nor outside, and are surfaced with that tag.
    """
    if precision == LocationPrecision.UNKNOWN or miles_away is None:
        return None
    if precision == LocationPrecision.APPROXIMATE:
        return miles_away <= max_miles + approximate_margin_miles
    return miles_away <= max_miles
