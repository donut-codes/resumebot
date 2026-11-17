"""What code may do with a pair of employer records that may be one employer: one table decides, first match wins, and
every decision comes with the words that say why ("Evidence, not a word that matches"). `employer_pairs.settle` (the
night, the tracker import, the researcher's last pass), `employer_pairs.propose_merge` (the researcher's word) and the
researcher's own pass all read this table, so no caller holds a rule of its own. Pure: no session and no record, only
what the pair finder worked out about the two records (`kinds`, `Ties`).

| Rule | Id                    | The pair                                                     | Action                 |
|------|-----------------------|--------------------------------------------------------------|------------------------|
| N1   | `decided`             | the owner or the researcher said keep separate/different     | never, not listed      |
| N2   | `agency`              | one is the staffing firm of the other (a "via" tag)          | never, not listed      |
| N3   | `contradicted`        | their websites are different domains, both confirmed         | never, not listed      |
| N4   | `brand`               | a name holds the other's in brackets; names not alike        | never (two employers)  |
| N5   | `tagged`              | a name carries a "via" tag that names no record              | never (two employers)  |
| M1   | `same_name`           | the two names read the same to `employers.key_of`            | merge                  |
| M2   | `website_and_names`   | they share a website, and their names are alike              | merge                  |
| M3   | `confirmed_website`   | they share a website both confirmed (owner, research)        | merge                  |
| M4   | `same_account`        | the same job-system account, and the names share a rare word | merge                  |
| A1   | `website_unconfirmed` | they share a website, and neither M2 nor M3 holds            | never (two employers)  |
| A2   | `names`               | their names are near-identical and nothing else matches      | never (two employers)  |
| A3   | `careers`             | they share a careers site and nothing else matches           | never (two employers)  |
|      | `nothing`             | nothing the app can check (the researcher's word only)       | never (two employers)  |

A pair that meets a merge rule is merged by code; every other pair is two employers, and the owner is never asked
about it (the owner's decision, 2026-11-13). Nothing is remembered for a pair the table keeps as two (N4, N5, A1 to A3,
`nothing`): if later evidence meets a merge rule (the researcher confirms both websites; a board turns out to be one
tenant) the pair is merged then, by code, without a question. Only N1 to N3 are left out of `employer_pairs.candidates`;
the others stay listed, because the researcher's brief offers them and what it finds can turn one into a merge.

Evidence, strongest first. The same job-system account (M4) is one customer's own tenant: a tenant host
(`acme.wd5.vendor.example`), a board slug on a hosted job system, a vendor host with the customer in it
(`careers-acme.vendor.example`), as `resolve.board_key` defines it, and not guessed from the employer's name; beside
it the names need share only one distinctive word (`employer_pairs.Rarity`: a word few of the owner's employers' names
hold, and not a descriptor like "Insurance"). The same registrable domain is evidence of whose it is once the owner or
the employer researcher has said so (M3): the per-role analyst sets one while reading a single posting, on a page that
can be the posting's own site. Beside names that are alike it is enough anyway (M2). Two confirmed websites on
different domains say the two are different employers (N3), so the pair is never merged. (A job board's company page
would be
evidence of the same kind, for and against; no record keeps one, because the researcher does not look for one, so
the table has no rule for it.)

No evidence at all, and so never a signal: the same public job board, and a job system's host with no tenant in it
(the root of a vendor's domain, a vendor's hosted careers platform), which every customer of that system shares
(`employer_pairs.domain_of`).

Names alone never merge. They are alike (`employer_pairs.names_alike`) when they are near-identical once legal endings
and punctuation are set aside: the same words, one name and the other plus only descriptor words ("Brightwater" and
"Brightwater Insurance"), or words that weigh nearly the same by how rare each is among the owner's employers. Then
they are two employers (A2) until other evidence says otherwise; any lesser likeness ("American Express" and
"American Express Global Business Travel", a shared "Summit") is no pair at all, left for the researcher. N5 is the
rule that was always there (a record whose name carries a "via" tag is never merged by code); N2 is its other half:
when the tag names the other record, the app knows the two are different employers.
"""

from collections.abc import Mapping
from dataclasses import dataclass

#: What the table says to do with a pair: merge it by code, or never (it is two employers as far as code is concerned).
MERGE, NEVER = "merge", "never"

#: The rules of the table, by id, in the order they are read.
DECIDED, AGENCY, CONTRADICTED, BRAND, TAGGED = "decided", "agency", "contradicted", "brand", "tagged"
SAME_NAME, WEBSITE_AND_NAMES, CONFIRMED_WEBSITE, SAME_ACCOUNT = (
    "same_name", "website_and_names", "confirmed_website", "same_account")
WEBSITE_UNCONFIRMED, NAMES, CAREERS, NOTHING = "website_unconfirmed", "names", "careers", "nothing"
#: No code reads this tuple: the tests do, to check that the table has a case for every rule.
RULES = (DECIDED, AGENCY, CONTRADICTED, BRAND, TAGGED, SAME_NAME, WEBSITE_AND_NAMES, CONFIRMED_WEBSITE, SAME_ACCOUNT,
         WEBSITE_UNCONFIRMED, NAMES, CAREERS, NOTHING)

#: Why code did not merge a pair: the words stay on the `Decision` (the researcher's brief, the tests).
_WHY = {
    DECIDED: "the owner or the researcher said they are different employers",
    AGENCY: "one is the staffing firm the other's roles come through",
    TAGGED: "one name says its roles come through a staffing firm, whose site and board are shared by the companies it "
            "hires for",
    WEBSITE_UNCONFIRMED: "it has not confirmed the website is the employer's own",
    NAMES: "a name alone does not show they are one employer",
    CAREERS: "a vendor can host the careers site of many employers",
    NOTHING: "nothing the app can check shows they are one employer",
}
#: What Activity says of a merge by the same name, and of a website the owner or the researcher confirmed.
_SAME_NAME_WORDS = "their names read the same once punctuation, brackets and endings like Inc. are set aside"
_CONFIRMED_WORDS = "which you or the researcher confirmed for both"


@dataclass(frozen=True)
class Ties:
    """What the pair finder worked out about two records beyond what they share (`decide`'s `kinds`), once:
    `decided` (N1: a finished question under the pair's key), `agency` (N2: a "via" tag of either names the other),
    `contradiction` (N3: the words that say how the two contradict each other: two confirmed websites on different
    domains), `brand` (N4: the brand's name and the parent's, when a name of one holds the other's in brackets and the
    two names are not alike), `tagged` (N5: either name carries a "via" tag), `same_name` (M1: the names read the same)
    and `confirmed` (M3: the website is the owner's or the researcher's word on both)."""

    decided: bool = False
    agency: bool = False
    contradiction: str | None = None
    brand: tuple[str, str] | None = None
    tagged: bool = False
    same_name: bool = False
    confirmed: bool = False


@dataclass(frozen=True)
class Decision:
    """What the table says of a pair: `action` (`MERGE` or `NEVER`), the `rule` that decided (an id of the table)
    and the `words` that say why: the clause Activity gives for a merge ("they share the website acme.test and their
    names are alike"), the reason code did not merge it otherwise."""

    action: str
    rule: str
    words: str


def decide(kinds: Mapping[str, str], ties: Ties) -> Decision:
    """The table. `kinds` is what the two records share, by kind (`employer_pairs._signals`: `website`, `careers`,
    `board`, `names` when the names are alike, `word` when they are not but share a distinctive word), each as a clause
    that finishes "they look the same because ..."; `ties` the rest. First match wins."""
    if ties.decided:
        return Decision(NEVER, DECIDED, _WHY[DECIDED])
    if ties.agency:
        return Decision(NEVER, AGENCY, _WHY[AGENCY])
    if ties.contradiction:
        return Decision(NEVER, CONTRADICTED, ties.contradiction)
    if ties.brand:
        brand, parent = ties.brand
        return Decision(NEVER, BRAND, f'"{brand}" holds "{parent}" in brackets, which looks like a brand and its '
                                      "parent")
    if ties.tagged:
        return Decision(NEVER, TAGGED, _WHY[TAGGED])
    named = "names" in kinds
    if ties.same_name:
        return Decision(MERGE, SAME_NAME, _SAME_NAME_WORDS)
    if "website" in kinds and named:
        return Decision(MERGE, WEBSITE_AND_NAMES, f"{kinds['website']} and their names are alike")
    if "website" in kinds and ties.confirmed:
        return Decision(MERGE, CONFIRMED_WEBSITE, f"{kinds['website']}, {_CONFIRMED_WORDS}")
    if "board" in kinds and named:
        return Decision(MERGE, SAME_ACCOUNT, f"{kinds['board']} and their names are alike")
    if "board" in kinds and "word" in kinds:
        return Decision(MERGE, SAME_ACCOUNT, f"{kinds['board']} and {kinds['word']}")
    if "website" in kinds:
        return Decision(NEVER, WEBSITE_UNCONFIRMED, _WHY[WEBSITE_UNCONFIRMED])
    if named:
        return Decision(NEVER, NAMES, _WHY[NAMES])
    if "careers" in kinds:
        return Decision(NEVER, CAREERS, _WHY[CAREERS])
    return Decision(NEVER, NOTHING, _WHY[NOTHING])
