"""A public id in an address's path is not a token (docs/PRD.md 30.3): only a user name or password and the query are
held against `find_secret`; free text keeps the whole-string check."""

import pytest

from app.agents.security import SecretRejected
from app.sites import addresses

KEY = "0123456789abcdef" * 2
UUID = "123e4567-e89b-12d3-a456-426614174000"
PORTAL = f"https://www.portal.example.test/v4/ats/web.php/portal/{KEY}/career-page"
SECRET = "sk-ant-api03-" + "a" * 40
#: Split so the export's secret scan does not read this made-up token as a real one.
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.mark.parametrize("address", [
    PORTAL,
    f"https://www.portal.example.test/v4/ats/web.php/portal/{KEY}/jobs/123456",
    f"https://recruiting.example.test/EXA1000EXAMP/JobBoard/{UUID}/",
    f"https://jobs.example.test/search?client={KEY}",
    f"https://jobs.example.test/search?client={UUID}&q=x",
    "https://jobs.example.test/jobs?q={phrase}",
])
def test_an_address_with_a_public_id_in_its_path_or_query_carries_no_token(address):
    assert addresses.address_secret(address) is None


@pytest.mark.parametrize("address", [
    f"https://jobs.example.test/jobs?key={SECRET}",
    f"https://jobs.example.test/jobs?token={JWT}",
    f"{PORTAL}?key={SECRET}",
    "https://user:pw@jobs.example.test/jobs",
    "https://user@jobs.example.test/jobs",
])
def test_a_password_or_a_token_in_the_query_is_a_secret(address):
    assert addresses.address_secret(address)


def test_a_string_that_is_no_address_is_left_to_the_public_address_check():
    assert addresses.address_secret("http://[bad") is None


def test_a_recipe_with_a_public_id_in_an_address_is_taken():
    addresses.reject_recipe_secrets({
        "kind": "page", "url": PORTAL, "links": "a.job",
        "detail": {"url": f"https://www.portal.example.test/portal/{KEY}/jobs/{{id}}"},
        "bookmarks": [{"url": PORTAL}], "start_url": PORTAL, "url_prefix": PORTAL})


@pytest.mark.parametrize("recipe", [
    {"kind": "page", "url": f"{PORTAL}?token={SECRET}"},
    {"kind": "request", "url": PORTAL, "body": {"key": SECRET}},
    {"kind": "page", "url": PORTAL, "notes": f"the page is {PORTAL}"},
    {"kind": "page", "bookmarks": [{"url": f"https://jobs.example.test/?k={SECRET}"}]},
])
def test_a_recipe_with_a_token_in_a_query_or_in_any_other_text_is_refused(recipe):
    with pytest.raises(SecretRejected) as caught:
        addresses.reject_recipe_secrets(recipe)
    assert caught.value.field == "recipe"
