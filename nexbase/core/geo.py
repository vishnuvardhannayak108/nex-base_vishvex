"""US geography shared by sources and normalization: state names, codes, parsing."""
from __future__ import annotations

import re

#: US state/territory names to the postal code the dataset usually stores.
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "washington dc": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "puerto rico": "PR", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_STATE_CODES = set(US_STATES.values())
#: The spelling to fall back on when a slice stores the name, not the code.
_STATE_NAME_OF = {}
for _name, _code in US_STATES.items():
    _STATE_NAME_OF.setdefault(_code, _name.title())
#: Parts that are a country, not a state.
_COUNTRY_PARTS = {
    "united states", "united states of america", "us", "usa", "u.s.", "u.s.a.",
}


def us_state_of(location: str | None) -> str | None:
    """The US state a posting is in, or None when it cannot be established.

    Handles the shapes the dataset stores: ``"Mason, OH"``,
    ``"Warner Robins, Georgia, United States"``, ``"Columbus, OH"`` with a
    trailing non-breaking space, and a bare ``"Ohio"``. A city is never read as
    a state while a later part still names one, so
    ``"Washington, District of Columbia"`` is DC, not WA.
    """
    if not location:
        return None
    # str.split() folds every kind of Unicode space, including the
    # non-breaking space the dataset leaves on rows like "Columbus, OH ".
    text = " ".join(str(location).split())
    parts = [part.strip().strip(".").strip() for part in text.split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return None
    # Right to left: the state sits after the city and before the country.
    # The city is only considered once nothing else has yielded a state, which
    # is what makes a bare "Ohio" work without turning "Washington, DC" into WA.
    for group in (parts[1:], parts[:1]):
        for part in reversed(group):
            value = part.lower()
            if value in _COUNTRY_PARTS:
                continue
            if len(part) == 2 and part.isalpha() and part.upper() in _STATE_CODES:
                return part.upper()
            if value in US_STATES:
                return US_STATES[value]
    return None


def in_us_state(location: str | None, state: str | None) -> bool:
    """True only when the posting positively names ``state``.

    A whole-token match, because the dataset's own filter is a plain substring
    and leaks badly: asked for "GA" it returns "Garden Grove, CA" and
    "Las Vegas, NV"; asked for "CO", "Costa Mesa" and "District of Columbia".

    Tokens rather than :func:`us_state_of`'s comma parsing, because a real row
    is often neither: "Fort Worth, TX (North)", "12803 West Ave, San Antonio,
    TX 78216", or a semicolon-separated list of forty sites. The code must be
    upper case as the dataset writes it, so the word "in" is not read as
    Indiana.
    """
    if not state:
        return True
    state = state.upper()
    text = " ".join(str(location or "").split())
    if not text:
        return False
    if re.search(rf"(?<![A-Za-z]){state}(?![A-Za-z])", text):
        return True
    names = [name for name, code in US_STATES.items() if code == state]
    return any(re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE)
               for name in names)
