"""Whether the name a posting gives is an employer's (docs/PRD.md 46.4, the Employer named dial): the owner's "No
anonymous postings. A posting must name a company", which the app can check on the name alone when the name is a
placeholder ("Confidential", "A leading healthcare company", "Fortune 500 company", a blank) or says its client is
hidden ("Staffco (client undisclosed)").

A name is judged only by its shape, never against a list of companies, and only for what it says of itself: an
address ("jobs.example.test") is a name of the site it is on, and a real-looking name stays one. What a posting's text
says of an unnamed client lives with the other facts read from a text (`gating.posting_facts`). A leaf: it reads no
database."""

import re

#: The longest name a reason quotes.
NAME_CHARS = 80
NO_COMPANY = "the posting gives no company"

#: Words that say the name is withheld, wherever they stand in it.
_WITHHELD = re.compile(
    r"\b(?:confidential|undisclosed|unnamed|withheld|not\s+(?:disclosed|provided|specified|named|listed|available|given))"
    r"\b", re.I)
#: Whole names that are no name.
_PLACEHOLDERS = frozenset({
    "anonymous", "anonymous employer", "anonymous company", "anonymous client", "anonymous posting", "unknown",
    "unknown company", "unknown employer", "n/a", "na", "none", "tbd", "tba", "company", "employer", "client",
    "hiring company", "hiring employer", "our client", "a client", "the client", "private company", "private employer",
    "company name", "employer name"})
_STEALTH = re.compile(r"^stealth(?:[\s-]+(?:mode|startup|start-up|company))*$", re.I)
#: "A leading healthcare company", "A global financial services firm", "Fortune 500 company": a description of an
#: employer in place of its name. The article (or a count of a list) is what tells it from a name ("Leading Edge Cafe").
_ADJECTIVE = (r"(?:leading|large|global|major|growing|fast[- ]growing|well[- ]known|well[- ]established|established|"
              r"prominent|top|national|international|regional|multi-?national|publicly[- ]traded|privately[- ]held|"
              r"mid-?sized|midsize|boutique|innovative|reputable|respected|top[- ]tier|renowned|premier)")
_NOUN = (r"(?:company|firm|organi[sz]ation|employer|corporation|business|enterprise|manufacturer|provider|startup|"
         r"start-up|bank|retailer|institution|client|carrier|brand|insurer|university|hospital|agency|network|system|"
         r"lender|distributor|supplier|developer|operator|nonprofit|non-profit)")
_DESCRIBED = re.compile(rf"^an?\s+(?:{_ADJECTIVE}\s+)+(?:[\w&/,-]+\s+){{0,4}}{_NOUN}$", re.I)
_LISTED = re.compile(rf"^(?:an?\s+)?(?:fortune|top|inc\.?)\s*\d+\s+(?:[\w&/-]+\s+){{0,3}}{_NOUN}$", re.I)


def hidden(name: str | None) -> str | None:
    """Why this name is no employer's, in words, or None when it is one: a blank, a name that says it is withheld, a
    placeholder, or a description of an employer given in the place of its name."""
    text = " ".join((name or "").split())
    if not text:
        return NO_COMPANY
    plain = text.strip(" .,;:!").lower()
    if (_WITHHELD.search(text) or plain in _PLACEHOLDERS or _STEALTH.match(plain) or _DESCRIBED.match(plain)
            or _LISTED.match(plain)):
        return f'the company is listed as "{text[:NAME_CHARS]}"'
    return None
