"""Which employer a confirmation names (`mail/reader.py`, docs/PRD.md 14.5): the whole name, not a word of another
company's name, and an alias of one word only with the role beside it. Made-up employers, titles and mail."""


from datetime import date

import pytest

from app.jobsearch.mail import reader as go_mail
from app.mailmine.message import Seen
from tests.jobsearch.made_up_sites_b import made_up_boards, made_up_mail  # noqa: F401 (used through pytestmark)

pytestmark = pytest.mark.usefixtures("made_up_mail")


def seen(subject: str, text: str = "", *, sender: str = "Careers", address: str = "no-reply@hr.example") -> Seen:
    domain = address.rsplit("@", 1)[-1]
    return Seen(sender_key=domain, sender_name=sender, subject=subject, day=date(2026, 10, 21), text=text, bulk=False,
                address=address)


def names(*own: str, aliases: tuple[str, ...] = ()) -> go_mail.Names:
    return go_mail.employer_names(list(own), list(aliases))


def test_a_word_of_the_name_inside_another_companys_name_is_not_the_employer():
    mine = names("Northfield Group, Inc.")
    mail = seen("Thank you for applying", "We have received your application to Harbor Northfield.",
                sender="Harbor Northfield", address="jobs@harbornorthfield.example")
    assert go_mail.match(mail, mine, "Data Analyst") is None


def test_the_whole_name_still_matches_whichever_legal_ending_the_mail_uses():
    mine = names("Northfield Group, Inc.")
    for ending in ("Northfield Group", "Northfield Group Companies", "Northfield Group Holdings, LLC"):
        mail = seen(f"Thank you for applying to {ending}", "We have received your application.")
        assert go_mail.match(mail, mine, "Data Analyst") == go_mail.UNSURE, ending


def test_a_name_of_two_words_matches_under_either_ending_of_the_legal_name():
    mine = names("Globex Insurance Group, Inc.")
    mail = seen("Thank you for applying", "We have received your application to Globex Insurance Companies.")
    assert go_mail.match(mail, mine, "Data Analyst") == go_mail.UNSURE


def test_the_sender_domain_names_the_employer_only_by_the_whole_name():
    mine = names("Northfield Group")
    assert go_mail.match(seen("Application received", address="jobs@northfieldgroup.example"),
                         mine, "Data Analyst") == go_mail.UNSURE
    assert go_mail.match(seen("Application received", address="jobs@harbornorthfield.example"),
                         mine, "Data Analyst") is None


def test_a_name_of_one_word_is_still_the_name():
    assert go_mail.match(seen("Thank you for applying to Quillon"), names("Quillon"), "Data Analyst") == go_mail.UNSURE


QUILLON = names("Quillon Insurance Group", aliases=("Quillon",))


def test_an_alias_of_one_word_alone_is_not_the_employer():
    mail = seen("Thank you for applying", "We have received your application to Quillon.")
    assert go_mail.match(mail, QUILLON, "Senior Data Analyst") is None


def test_an_alias_of_one_word_names_the_employer_when_the_mail_names_the_role_too():
    mail = seen("Thank you for applying", "We have received your application for Data Analyst at Quillon.")
    assert go_mail.match(mail, QUILLON, "Senior Data Analyst") == go_mail.UNSURE


def test_an_alias_of_one_word_with_the_whole_title_is_a_sure_match():
    mail = seen("Thank you for applying", "We have received your application for Senior Data Analyst at Quillon.")
    assert go_mail.match(mail, QUILLON, "Senior Data Analyst") == go_mail.SURE


def test_one_word_of_a_longer_title_is_not_the_role():
    mail = seen("Thank you for applying", "We have received your application for Analyst at Quillon.")
    assert go_mail.match(mail, QUILLON, "Senior Data Analyst") is None


def test_a_title_of_one_word_is_the_role_when_the_mail_holds_it():
    mail = seen("Thank you for applying", "We have received your application for Actuary at Quillon.")
    assert go_mail.match(mail, QUILLON, "Actuary") == go_mail.SURE


def test_the_senders_domain_does_not_make_an_alias_of_one_word_the_employer():
    assert go_mail.match(seen("Application received", address="jobs@quillon.example"), QUILLON, "Data Analyst") is None


def test_the_employers_own_name_matches_without_the_role():
    mail = seen("Thank you for applying", "We have received your application to Quillon Insurance.")
    assert go_mail.match(mail, QUILLON, "Senior Data Analyst") == go_mail.UNSURE


def test_an_alias_of_two_words_is_a_name_like_any_other():
    mine = names("Quillon Insurance Group", aliases=("Quillon Mutual",))
    mail = seen("Thank you for applying", "We have received your application to Quillon Mutual.")
    assert go_mail.match(mail, mine, "Senior Data Analyst") == go_mail.UNSURE


def test_an_alias_that_is_the_same_name_as_the_employers_is_not_loose():
    mine = names("Quillon", aliases=("Quillon, Inc.",))
    assert mine.loose == [] and mine.keys == ["quillon"]


def test_best_reads_the_alias_rule_too():
    mails = [seen("Thank you for applying", "We have received your application to Quillon."),
             seen("Application received", "Your application for Data Analyst at Quillon was received.")]
    said, found = go_mail.best(mails, QUILLON, "Senior Data Analyst")
    assert said == go_mail.UNSURE and found is mails[1]


def test_an_employers_own_site_is_still_a_name():
    mine = names("fabrikam.example")
    assert go_mail.match(seen("Thank you for applying", "Your application to fabrikam.example was received."), mine,
                         "Data Analyst") == go_mail.UNSURE
