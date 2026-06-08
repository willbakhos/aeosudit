"""Detect whether an IndustryReport is a local-services ranking and, if so,
classify the vertical to the right Schema.org local-business subtype.

Used by /ai-visibility/{slug} pages to emit typed ItemList items + an
areaServed annotation, so Google reads the page as a *directory of local
businesses* (the pattern Yelp / G2 / Avvo use) rather than a generic
research dataset. This is purely additive schema — the existing Dataset,
Article and FAQPage blocks are untouched. Non-local industries (SaaS,
Marketing, Security, etc.) get the same Organization-typed items as before.
"""
from __future__ import annotations

import re
from typing import Any

# Parent categories where city-named industries are nearly always
# local-services. Used as a coarse gate so we don't accidentally classify a
# SaaS "Sign-in tools" or marketing "All-in-one platforms" as local because
# their name happens to contain " in ".
_LOCAL_PARENT_CATEGORIES: set[str] = {
    "Healthcare",
    "Legal",
    "Real Estate",
    "Business Services",
    "Hospitality",
}

# Vertical-pattern → Schema.org type. Matched against the vertical portion
# of the industry name (e.g. "Family lawyers" out of "Family lawyers in
# Los Angeles"). First substring match wins, so put longer patterns first
# (e.g. "personal injury lawyer" before "lawyer").
#
# Schema types chosen for direct local-business signal — Google's local
# pack recognises these subtypes directly. LocalBusiness is the generic
# fallback when nothing more specific applies.
_VERTICAL_TYPE_MAP: list[tuple[str, str]] = [
    ("personal injury lawyer", "LegalService"),
    ("family lawyer", "LegalService"),
    ("lawyer", "LegalService"),
    ("attorney", "LegalService"),
    ("orthodontist", "Dentist"),
    ("dentist", "Dentist"),
    ("dermatologist", "Physician"),
    ("plastic surgeon", "Physician"),
    ("physician", "Physician"),
    ("doctor", "Physician"),
    ("chiropractor", "Physician"),
    ("veterinarian", "VeterinaryCare"),
    ("med spa", "MedicalBusiness"),
    ("medical spa", "MedicalBusiness"),
    ("coworking space", "LocalBusiness"),
    ("coworking", "LocalBusiness"),
    ("real estate agent", "RealEstateAgent"),
    ("realtor", "RealEstateAgent"),
    ("accountant", "AccountingService"),
    ("plumber", "Plumber"),
    ("electrician", "Electrician"),
    ("hvac", "HVACBusiness"),
    ("auto repair", "AutoRepair"),
    ("mechanic", "AutoRepair"),
    ("gym", "ExerciseGym"),
    ("fitness", "ExerciseGym"),
    ("hair salon", "BeautySalon"),
    ("salon", "BeautySalon"),
    ("day spa", "DaySpa"),
    ("spa", "DaySpa"),
    ("florist", "Florist"),
    ("restaurant", "Restaurant"),
    ("cafe", "CafeOrCoffeeShop"),
]

# Minimal city → state lookup for the US cities currently in the dataset.
# Extend as new cities are added. The map enables a richer areaServed
# annotation (`containedInPlace`) which is the same shape Yelp uses to
# disambiguate "Springfield" etc. When a city isn't here we still emit
# the city — just without the state node.
_CITY_TO_STATE: dict[str, tuple[str, str]] = {
    # (city_lower → (Proper Case Name, USPS state abbreviation, full state name))
    # value tuple: (canonical_city, state_abbr, state_full)
}
# Populate via concise data table — preserved as tuples so editing is easy.
_CITY_DATA = [
    ("New York", "NY", "New York"),
    ("Los Angeles", "CA", "California"),
    ("Chicago", "IL", "Illinois"),
    ("Houston", "TX", "Texas"),
    ("Phoenix", "AZ", "Arizona"),
    ("Philadelphia", "PA", "Pennsylvania"),
    ("San Antonio", "TX", "Texas"),
    ("San Diego", "CA", "California"),
    ("Dallas", "TX", "Texas"),
    ("Austin", "TX", "Texas"),
    ("Jacksonville", "FL", "Florida"),
    ("Fort Worth", "TX", "Texas"),
    ("Columbus", "OH", "Ohio"),
    ("Charlotte", "NC", "North Carolina"),
    ("San Francisco", "CA", "California"),
    ("Indianapolis", "IN", "Indiana"),
    ("Seattle", "WA", "Washington"),
    ("Denver", "CO", "Colorado"),
    ("Washington", "DC", "District of Columbia"),
    ("Boston", "MA", "Massachusetts"),
    ("El Paso", "TX", "Texas"),
    ("Nashville", "TN", "Tennessee"),
    ("Detroit", "MI", "Michigan"),
    ("Oklahoma City", "OK", "Oklahoma"),
    ("Portland", "OR", "Oregon"),
    ("Las Vegas", "NV", "Nevada"),
    ("Memphis", "TN", "Tennessee"),
    ("Louisville", "KY", "Kentucky"),
    ("Baltimore", "MD", "Maryland"),
    ("Milwaukee", "WI", "Wisconsin"),
    ("Albuquerque", "NM", "New Mexico"),
    ("Tucson", "AZ", "Arizona"),
    ("Fresno", "CA", "California"),
    ("Sacramento", "CA", "California"),
    ("Mesa", "AZ", "Arizona"),
    ("Kansas City", "MO", "Missouri"),
    ("Atlanta", "GA", "Georgia"),
    ("Miami", "FL", "Florida"),
    ("Raleigh", "NC", "North Carolina"),
    ("Omaha", "NE", "Nebraska"),
    ("Long Beach", "CA", "California"),
    ("Virginia Beach", "VA", "Virginia"),
    ("Oakland", "CA", "California"),
    ("Minneapolis", "MN", "Minnesota"),
    ("Tulsa", "OK", "Oklahoma"),
    ("Tampa", "FL", "Florida"),
    ("Arlington", "TX", "Texas"),
    ("New Orleans", "LA", "Louisiana"),
    ("Cleveland", "OH", "Ohio"),
    ("Honolulu", "HI", "Hawaii"),
    ("Anaheim", "CA", "California"),
    ("Pittsburgh", "PA", "Pennsylvania"),
    ("St. Louis", "MO", "Missouri"),
    ("Cincinnati", "OH", "Ohio"),
    ("Anchorage", "AK", "Alaska"),
    ("Orlando", "FL", "Florida"),
]
for _city, _abbr, _full in _CITY_DATA:
    _CITY_TO_STATE[_city.lower()] = (_city, _abbr, _full)

# US state names → (canonical name, USPS abbreviation). Used so that
# state-level pages like "Dentists in Texas" emit areaServed as a State
# (not a City named "Texas"). Includes DC. Lookup is case-insensitive.
_STATE_DATA = [
    ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"),
    ("California", "CA"), ("Colorado", "CO"), ("Connecticut", "CT"),
    ("Delaware", "DE"), ("Florida", "FL"), ("Georgia", "GA"), ("Hawaii", "HI"),
    ("Idaho", "ID"), ("Illinois", "IL"), ("Indiana", "IN"), ("Iowa", "IA"),
    ("Kansas", "KS"), ("Kentucky", "KY"), ("Louisiana", "LA"), ("Maine", "ME"),
    ("Maryland", "MD"), ("Massachusetts", "MA"), ("Michigan", "MI"),
    ("Minnesota", "MN"), ("Mississippi", "MS"), ("Missouri", "MO"),
    ("Montana", "MT"), ("Nebraska", "NE"), ("Nevada", "NV"),
    ("New Hampshire", "NH"), ("New Jersey", "NJ"), ("New Mexico", "NM"),
    ("New York", "NY"), ("North Carolina", "NC"), ("North Dakota", "ND"),
    ("Ohio", "OH"), ("Oklahoma", "OK"), ("Oregon", "OR"),
    ("Pennsylvania", "PA"), ("Rhode Island", "RI"), ("South Carolina", "SC"),
    ("South Dakota", "SD"), ("Tennessee", "TN"), ("Texas", "TX"),
    ("Utah", "UT"), ("Vermont", "VT"), ("Virginia", "VA"),
    ("Washington State", "WA"),  # disambiguation: "Washington" alone is DC city
    ("West Virginia", "WV"), ("Wisconsin", "WI"), ("Wyoming", "WY"),
    ("District of Columbia", "DC"),
]
_STATE_NAME_TO_ABBR: dict[str, tuple[str, str]] = {
    full.lower(): (full, abbr) for full, abbr in _STATE_DATA
}


def _classify_vertical(vertical: str) -> str | None:
    """Substring match the lowercased vertical against the type map.
    Returns the matching Schema.org type or None if no match.

    Singular/plural is folded by simple suffix strip — "Lawyers" → "lawyer",
    "Dermatologists" → "dermatologist". Compound matches (e.g. "Family
    lawyers" → "family lawyer") are caught because longer patterns are
    listed first."""
    v = vertical.lower().strip()
    # Crude singular-isation: trip trailing 's' on the LAST word
    parts = v.split()
    if parts and parts[-1].endswith("s") and len(parts[-1]) > 2:
        parts[-1] = parts[-1][:-1]
    v_singular = " ".join(parts)

    for pattern, schema_type in _VERTICAL_TYPE_MAP:
        if pattern in v or pattern in v_singular:
            return schema_type
    return None


def _parse_city_state(locality: str) -> tuple[str | None, str | None, str | None]:
    """Parse a locality string like "Chicago", "Washington, DC", "Texas" or
    "Los Angeles" into (city_name, state_abbr, state_full).

    Returns city_name=None when the locality is a US state name (so the
    caller emits areaServed as a State instead of a City). Returns all
    None when the locality is empty / malformed."""
    s = locality.strip()
    if not s:
        return None, None, None
    # "Washington, DC" → city + explicit state abbr
    if "," in s:
        city_part, state_part = (p.strip() for p in s.split(",", 1))
        if not city_part:  # malformed "Lawyers in ,"
            return None, None, None
        # Look up canonical state name from the abbr if we have it
        for canon_city, abbr, full in _CITY_DATA:
            if canon_city.lower() == city_part.lower() and abbr.lower() == state_part.lower():
                return canon_city, abbr, full
        # Fall back: keep what we parsed
        state_abbr = state_part.upper() if (state_part and len(state_part) <= 3) else None
        return city_part, state_abbr, None
    # No comma. First check: is this actually a US state name?
    # If so, emit it as a State (city=None) — not as a City named "Texas".
    state_lookup = _STATE_NAME_TO_ABBR.get(s.lower())
    if state_lookup:
        full, abbr = state_lookup
        return None, abbr, full
    # Otherwise treat as a city — look up in city map for canonical form
    looked_up = _CITY_TO_STATE.get(s.lower())
    if looked_up:
        canon_city, abbr, full = looked_up
        return canon_city, abbr, full
    return s, None, None


_NAME_RE = re.compile(r"^(.+?)\s+in\s+(.+)$", re.IGNORECASE)


def detect_local_services(
    slug: str,
    name: str,
    parent_category: str | None,
) -> dict[str, Any] | None:
    """Return a dict describing how to type the ranking, or None if this
    industry isn't a local-services page.

    Returned dict shape:
        {
            "schema_type": "LegalService" | "Dentist" | ...,
            "vertical": "Lawyers",          # the X in "X in Y"
            "city_name": "Chicago" | None,  # None when Y is a state-only page
            "state_abbr": "IL" | None,
            "state_full": "Illinois" | None,
            "area_type": "City" | "State",  # what areaServed should be typed as
        }

    Detection requires TWO conditions to avoid false positives:
      1. parent_category is in the local-services-likely set, AND
      2. name matches the "X in Y" pattern AND classifies to a known
         local-services vertical OR has a known US city/state in Y.

    The double check prevents misclassifying a SaaS like "Sign-in tools"
    or a marketing tool like "Lead capture forms in Salesforce" as
    local-services. Empty/malformed locality strings return None — better
    to keep the existing Organization schema than emit a broken City.
    """
    if not name or not parent_category:
        return None
    if parent_category not in _LOCAL_PARENT_CATEGORIES:
        return None

    m = _NAME_RE.match(name.strip())
    if not m:
        return None
    vertical_raw = m.group(1).strip()
    locality_raw = m.group(2).strip()
    if not vertical_raw or not locality_raw:
        return None

    city_name, state_abbr, state_full = _parse_city_state(locality_raw)
    schema_type = _classify_vertical(vertical_raw)

    # Require EITHER a recognised vertical OR a recognised state/city to
    # confirm this is genuinely a local-services page.
    if not schema_type and not state_abbr:
        return None

    # Refuse to emit broken schema. If neither a city nor a state could
    # be parsed (e.g. "Lawyers in xyz123"), the existing Organization
    # schema is strictly safer than a typed item with no areaServed.
    if not city_name and not state_abbr:
        return None

    # city_name=None + state set → state-only page, areaServed is a State
    area_type = "State" if (city_name is None and state_abbr) else "City"

    return {
        "schema_type": schema_type or "LocalBusiness",
        "vertical": vertical_raw,
        "city_name": city_name,
        "state_abbr": state_abbr,
        "state_full": state_full,
        "area_type": area_type,
    }
