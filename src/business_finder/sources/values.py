"""Turning listing sites' text values into numbers.

Shared by the source adapters, so "$1,250,000", "633,484", "N/A",
"Call/Email" and the like are read the same way everywhere.
"""

from datetime import UTC, datetime

# Years in business above this are treated as a year, not a count.
MAX_YEARS_IN_BUSINESS = 150
EARLIEST_PLAUSIBLE_YEAR = 1800


def parse_dollar_amount(value: object) -> int | None:
    """Turn a listing's money value into whole dollars, or None if not stated.

    Sites mix numbers (450000), digit strings ("1450000"), formatted
    strings ("633,484") and placeholders ("Call/Email", "All Cash"). BizBen also
    uses 0 where the seller left a field blank, so 0 is treated as "not
    stated": no business for sale has zero revenue or a zero asking price.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        amount = int(value)
    elif isinstance(value, str):
        digits_only = value.replace("$", "").replace(",", "").strip()
        if not digits_only.isdigit():
            return None
        amount = int(digits_only)
    else:
        return None
    return amount if amount > 0 else None


def parse_whole_number(value: object) -> int | None:
    """Turn a number or digit string into an int, or None if it isn't one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def is_years_in_business_count(value: object) -> bool:
    """Return True when an "established" value is a count of years, not a year.

    The field usually holds a year ("2009"), but some sellers type how many
    years the business has been running ("5"). A number this small can't
    be a founding year, so it is read as a count.
    """
    number = parse_whole_number(value)
    return number is not None and 0 < number <= MAX_YEARS_IN_BUSINESS


def parse_year_established(value: object, current_year: int | None = None) -> int | None:
    """Return the year a business was established, if it can be told.

    A count of years ("5") becomes a founding year counted back from
    `current_year`; it can be a year off, which is fine for a minimum-age
    filter. Anything that is neither a count nor a plausible year is dropped.
    """
    if current_year is None:
        current_year = datetime.now(UTC).year

    number = parse_whole_number(value)
    if number is None:
        return None
    if is_years_in_business_count(number):
        return current_year - number
    if EARLIEST_PLAUSIBLE_YEAR <= number <= current_year:
        return number
    return None
