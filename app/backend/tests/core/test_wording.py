"""Owner-facing wording lives in `templates/wording.<group>.yaml` (`app.harness.wording`): every placeholder in each
file is filled by the code that uses it, and a typo fails here."""

import pytest

from app.harness import wording
from app.harness.instructions import MissingPlaceholder, placeholders
from app.jobsearch.missions import core as missions
from app.jobsearch.owner_text import ROOM_CHARS, too_large_words
from app.settings import settings

#: The values each entry's code fills it with (the keys are the placeholders it may name).
FILLED = {
    ("missions", "purpose_sweep_stuck"): {"stuck_minutes"},
    ("owner_text", "too_large_alone"): {"what", "size", "room", "result", "fix"},
    ("owner_text", "too_large_beside"): {"what", "size", "room", "result", "fix", "other_name", "other_size"},
}


def test_every_placeholder_in_every_wording_file_is_one_its_code_fills():
    assert set(wording.groups()) == {"how_they_work", "missions", "owner_text"}
    for name in wording.groups():
        for key, text in wording.group(name).items():
            assert placeholders(text) <= FILLED.get((name, key), set()), f"{name}.{key} names an unfilled placeholder"


def test_a_placeholder_with_no_value_fails_loudly():
    with pytest.raises(MissingPlaceholder):
        wording.say("missions", "purpose_sweep_stuck")


def test_an_unknown_name_or_group_is_refused_naming_the_file():
    with pytest.raises(wording.WordingError, match="no entry named 'nope'"):
        wording.say("missions", "nope")
    with pytest.raises(wording.WordingError, match="wording.nothing.yaml"):
        wording.group("nothing")


def test_every_mission_has_its_purpose_filled_from_its_file():
    for key in missions.KINDS:
        purpose = missions.kind_of(key).purpose()
        values = {"stuck_minutes": settings.stuck_minutes} if key == missions.SWEEP else {}
        assert purpose == wording.say("missions", f"purpose_{key}", **values)
        assert "{{" not in purpose


def test_the_too_large_sentences_read_as_they_always_did():
    assert too_large_words("Your text", 214_000, room=150_000, result="it was not sent.", fix="Shorten it.") == (
        "Your text is 214,000 characters. An agent can be sent at most 150,000 at once, so it was not sent. Shorten it."
    )
    assert too_large_words("Your answer", 9_000, beside=("the file sent with it", 20_000)) == (
        "Your answer is 9,000 characters, and the file sent with it is 20,000. An agent can be sent at most "
        f"{ROOM_CHARS:,} of the two at once, so it was not sent. Nothing was cut and nothing was saved. Shorten it."
    )
