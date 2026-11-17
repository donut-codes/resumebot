"""A board is one employer's own (docs/PRD.md 28.1 and 28.8): its tenant, id, slug or host on a job system, never the
system's root host, and never one two employers hold, which would read it twice. A link to a vendor's front page, its
sign-in or its assets is not a board; two employers whose records point at one board do not both get it, and the one
whose name it carries (or the owner's own word) keeps it; a board two employers already hold is taken from the one it
is not, and a careers page does not give an employer a board another holds. Every employer and address is made up
(example.test, or a name no one has); no network."""


from app.jobsearch.employers import core as employers
from app.jobsearch.employers import resolve
from app.jobsearch.models import Employer


def test_a_name_is_on_a_board_when_both_are_read_the_same_way_whatever_joins_the_words():
    def employer(name: str) -> Employer:
        return Employer(key=employers.key_of(name), name=name, aliases=[])

    owns = resolve.owns
    assert owns(employer("Quillon & Marlowe"), "feedtwo", {"slug": "quillon-and-marlowe"})
    assert owns(employer("Quillon and Marlowe"), "feedtwo", {"slug": "Quillon_And_Marlowe"})
    assert owns(employer("Quillon & Marlowe"), "tenantone",
                {"tenant": "quillon", "shard": "wd5", "site": "Quillon_and_Marlowe"})
    assert owns(employer("Quillon & Marlowe"), "feedtwo", {"slug": "quillonmarlowe"}), "run together is still its name"
    assert owns(employer("Sandstone Partners"), "feedtwo", {"slug": "sandstone-partners"}), "a word holding 'and' stays"
    assert owns(employer("Anderson"), "feedtwo", {"slug": "anderson"})
    assert not owns(employer("Quillon & Marlowe"), "feedtwo", {"slug": "quillon-and-fernwood"})
    assert not owns(employer("Fernwood"), "feedtwo", {"slug": "and-the-group"}), "only words that are noise say nothing"
