"""Made-up documents in the shapes earlier releases saved, for the test of migration 0049 (which writes every stored
version in today's shape). Nothing here is real data. The app no longer reads these shapes: only the migration
does (tests/test_stored_versions_migration.py)."""

from tests.support import LAKESIDE, SPRINGDALE

#: Gates as the app saved them while each gate had a switch of its own and the work location had places with names that
#: count (before a switch on each section).
PER_ITEM_GATES = {
    "location": {
        "on": True, "remote": {"on": True, "countries": []},
        "hybrid": {"on": True, "places": [SPRINGDALE, LAKESIDE]},
        "in_person": {"on": True, "places": [SPRINGDALE, LAKESIDE]},
    },
    "compensation": {"on": True, "amount": 95_000, "against": "ask", "wide_band_ratio": 1.5},
    "fit": {"on": True, "percent": None, "score": 1},
    "other": [{"id": "travel", "kind": "rule", "on": True, "section": "description", "text": "Travel of 20% or more."}],
    "employer_names": ["Acme Holdings", "Acme", "Globex"],
    "notes": [],
}

#: The same made-up owner's criteria and gates as they were saved before the gates were grouped (a flat list of gates;
#: the places and remote in the criteria), with every kind of leftover the conversion must carry: a gate's note, a
#: place's note, words on the pay, fit and employer gates, a second gate of a kind.
OLD_CRITERIA = {
    "positioning": "A director who builds programs.",
    "search_terms": ["Programs", "Operations"],
    "places": [
        {"id": "springdale", "name": "Springdale area", "role": "home",
         "towns": ["Oakfield", "Riverton", "Maple Heights", "Springdale"], "note": "Where we live."},
        {"id": "lakeside", "name": "Lakeside, IL", "role": "also"},
        {"id": "gotham", "name": "Gotham", "role": "avoid", "towns": ["Greater Gotham"], "note": "Not even hybrid."},
    ],
    "remote": {"ok": True, "note": "Fully remote is best.", "countries": ["Canada", "United States"]},
}
OLD_GATES = {
    "gates": [
        {"id": "never", "kind": "employer", "employers": ["Acme Holdings", "Acme", "Globex"],
         "text": "Staffing firms that hide the employer.", "note": "Since the layoffs."},
        {"id": "where", "kind": "place", "several_places": "any_fails", "note": "Hybrid counts as on site."},
        {"id": "pay", "kind": "pay_floor", "amount": 95_000, "wide_band_ratio": 1.8, "text": "No commission-only pay."},
        {"id": "old-pay", "kind": "pay_floor", "on": False, "amount": 80_000},
        {"id": "fit", "kind": "fit_minimum", "percent": 55, "text": "A role that is mostly meetings."},
        {"id": "travel", "kind": "rule", "text": "Travel of 20% or more.", "note": "Learned the hard way."},
        {"id": "age", "kind": "posting_age", "days": 45, "on": False},
        {"id": "never-pe", "kind": "employer_fact", "fact": "ownership", "op": "!=", "value": "pe"},
    ],
    "notes": [{"id": "earlier", "text": "A note that was already here.", "source": "old-profile.md"}],
}

#: Scoring as it was saved before asks: a base score with levels, modifiers of several kinds in groups.
OLD_SCORING = {
    "scale_min": 1, "scale_max": 4,
    "levels": [{"score": 4, "means": "building is the job"}, {"score": 1, "means": "neither"}],
    "modifiers": [
        {"id": "aging", "kind": "posting_age", "says": "Posted more than 30 days ago", "points": -1, "days": 30,
         "checked_by": "app"},
        {"id": "promotion", "kind": "title", "says": "A promotion", "points": 1, "group": "level"},
    ],
    "groups": [{"id": "level", "name": "Title level"}],
}

#: A made-up scoring document in the older shape: a 1 to 4 base score, modifiers of every kind, flags, an order of work.
OLD_SCORING_FULL = {
    "measures": "How much of the job is building.",
    "scale_min": 1, "scale_max": 4,
    "levels": [{"score": 4, "means": "building is the job"}, {"score": 1, "means": "none of it"}],
    "cap": 7,
    "modifiers": [
        {"id": "up", "kind": "title", "says": "A promotion: Director or Head of", "points": 2, "group": "level"},
        {"id": "down", "kind": "title", "says": "A step down", "points": -1, "group": "level"},
        {"id": "nearby", "kind": "place", "says": "On site in the home area", "points": 1},
        {"id": "aging", "kind": "posting_age", "says": "Posted over 30 days ago", "points": -1, "days": 30,
         "checked_by": "app"},
        {"id": "pay-high", "kind": "pay", "says": "Ask over 150,000", "points": 2, "amount": 150000, "above": True,
         "group": "pay"},
        {"id": "domain", "kind": "industry", "says": "In a field the owner knows", "points": 1},
        {"id": "small", "kind": "employer", "says": "Under 1,000 people", "points": 1, "fact": "employees",
         "op": "<", "value": 1000},
        {"id": "gaps", "kind": "requirements", "says": "Each unproven required item", "points": -1,
         "per_item": True, "limit": -3, "from_classes": "U"},
        {"id": "leads", "kind": "rule", "says": "The role leads a team", "points": 1},
        {"id": "perks", "kind": "rule", "says": "The benefits are strong", "points": 1, "on": False},
        {"id": "both", "kind": "rule", "says": "Responsibilities and qualifications agree", "points": 1},
    ],
    "groups": [{"id": "level", "name": "Title level", "note": "one title level"}, {"id": "pay", "name": "Pay tier"}],
    "flags": [{"id": "pe", "says": "Owned by private equity"}, {"id": "notes-on-qualifications",
                                                                "says": "The qualifications name a licence"}],
    "priority": [{"id": "near", "rank": 2, "says": "Nearby first"}, {"id": "new", "rank": 1, "says": "Newest first"}],
    "evidence": "base 4; promotion +2 = 6",
    "notes": [{"id": "kept", "text": "An earlier note.", "source": "old-profile"}],
}
