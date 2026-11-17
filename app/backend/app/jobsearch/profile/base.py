"""What every profile document is built from: the kinds, `Part` and `Note`, and the outline of a posting that What to
look for, Gates and Scoring are all written by. Nothing here holds anyone's preferences. See `profile/schema.py`."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.jobsearch.profile import gate_words

KINDS = ("criteria", "gates", "scoring", "settings")


class Part(BaseModel):
    """Every list item has a stable id, so history can name what changed ("pay floor: 95,000 to 120,000")."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")


class Note(Part):
    """Something from the owner's old profile that no field holds yet, kept word for word until they place it."""

    text: str
    source: str = ""


# --- the outline: the parts of a posting that What to look for, Gates and Scoring are all written by -----------------

#: The parts of a posting, in the order a posting reads, with the words the pages show (docs/PRD.md 30.4 to 30.6). What
#: to look for, Gates and Scoring all follow this outline: an ask or a gate belongs to one of them. The ids are what is
#: stored and stay as they are, whatever the page calls a part.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("company", "Company"),
    ("posting", "Job posting"),
    ("description", "Description"),
    ("responsibilities", "Responsibilities"),
    ("qualifications", "Qualifications"),
    ("preferred", "Preferred"),
    ("compensation", "Benefits / Compensation"),
)
SECTION_IDS = tuple(s for s, _ in SECTIONS)
SECTION_LABEL = dict(SECTIONS)
SectionId = Literal["company", "posting", "description", "responsibilities", "qualifications", "preferred",
                    "compensation"]
assert SECTION_IDS == gate_words.SECTION_IDS  # one outline, written once (gate_words.py)


#: How a fact is compared with a value, for an ask or an employer-fact gate.
#: `in` is a dial's pick list: the fact is one of the listed values, comma-separated (docs/PRD.md 46).
FactOp = Literal[">=", "<=", ">", "<", "=", "!=", "contains", "excludes", "in"]
