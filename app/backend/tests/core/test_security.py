"""The secret filter every agent-written field passes (`agents/security.py`): what `find_secret` refuses, what
`redact_secrets` and `scrub` hide, what `reject_secrets` names, and the small text helpers (`textutil.py`) the
fields are shaped with. Every key and token here is made up."""

import hashlib

import pytest

from app import textutil
from app.agents import security
from app.agents.security import REMOVED, SecretRejected, TooLong
from app.errors import DomainError

#: One made-up token of each well-known shape, and the first eight characters the refusal quotes.
KEYS = [
    ("sk-abcdefghij1234567890", "sk-abcde"),
    ("sk-ant-api03-abcdefghij1234567890", "sk-ant-a"),
    ("ghp_A1b2C3d4E5f6G7h8I9j0K1l2", "ghp_A1b2"),
    ("gho_A1b2C3d4E5f6G7h8I9j0K1l2", "gho_A1b2"),
    ("github_pat_11ABCDEFG0abcdefghij_klmnopqrst", "github_p"),
    ("AKIAABCDEFGHIJKLMNOP", "AKIAABCD"),
    ("ASIAABCDEFGHIJKLMNOP", "ASIAABCD"),
    ("xoxb-1234567890-abcdefghij", "xoxb-123"),
    ("AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2", "AIzaSyA1"),
    ("npm_A1b2C3d4E5f6G7h8I9j0K1l2", "npm_A1b2"),
]
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1rwW1gFWFOEjXk"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAyA1b2C3d4\n-----END RSA PRIVATE KEY-----"
RANDOM = "q8Zk3VhD0xT7mNwP2cRb9LfY6uJaE4sXgH1oKiB5tUe"


# --- find_secret ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_nothing_is_a_secret_when_there_is_nothing(empty):
    assert security.find_secret(empty) is None


def test_a_private_key_block_is_refused_whatever_the_kind_of_key():
    for kind in ("RSA ", "EC ", "OPENSSH ", ""):
        assert security.find_secret(f"-----BEGIN {kind}PRIVATE KEY-----\nabc") == "looks like a PEM private key block"
    public = "-----BEGIN PUBLIC KEY-----\nMFwwDQYJKoZIhvcNAQEBBQADSwAw"
    assert security.find_secret(public) is None, "a public key is fine"
    assert security.find_secret(f"here it is:\n{PEM}\nplease use it") == "looks like a PEM private key block"


def test_a_jwt_is_refused_but_a_dotted_version_or_a_domain_is_not():
    assert security.find_secret(f"Authorization: Bearer {JWT}") == "looks like a JWT"
    assert security.find_secret("version 1.22.333 of boards.example.test") is None
    assert security.find_secret("eyJ.short.parts") is None


@pytest.mark.parametrize("key, shown", KEYS)
def test_a_key_with_a_well_known_prefix_is_refused_and_only_its_first_characters_are_quoted(key, shown):
    reason = security.find_secret(f"use {key} for the board")
    assert reason == f"looks like an API key ({shown}...)"
    assert key not in reason, "the refusal never repeats the secret"


@pytest.mark.parametrize("text", [
    "sk-short", "ghp_tooshort", "AKIA123", "skeleton-key-for-the-door", "the task-list is long", "npm_ is a prefix"])
def test_a_word_that_only_starts_like_a_key_is_not_one(text):
    assert security.find_secret(text) is None


def test_a_long_random_run_is_refused_whether_it_is_hex_or_base64_and_a_repeating_one_is_not():
    assert security.find_secret(RANDOM) == "looks like a long random token"
    assert security.find_secret(hashlib.sha256(b"made up").hexdigest()) == "looks like a long random token"
    assert security.find_secret("0" * 64) is None and security.find_secret("ab" * 30) is None
    assert security.find_secret("a" * 80) is None, "the same letter over and over is no secret"
    assert security.find_secret("0123456789" * 4) is None, "a hex run needs letters and digits"


def test_ordinary_text_addresses_and_ids_are_clean():
    for text in (
            "The role leads a team of eight and reports to the board each quarter.",
            "https://boards.example.test/acme/jobs/4012345678?gh_src=abc123",
            "123e4567-e89b-12d3-a456-426614174000",
            "Requires 5+ years: Python, SQL, and AWS (Lambda, S3).",
            "See en-US/External_Career_Site/job/Remote-USA/Director-of-Programs_R-102345",
    ):
        assert security.find_secret(text) is None, text


CAMEL_PATHS = (
    "https://fa-example-prod1.jobs.example.test/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
    "https://fa-example-prod1.jobs.example.test/hcmUI/CandidateExperience/en/sites/CX_1001/job/12345",
    "https://careers.example.test/careers/SearchJobs/DataAnalyst/OpenPositionsInTheNorthernRegion",
)
RANDOM_SHAPES = (
    "aZ3kQ9xLm2PvB7nRt5YwC8dFh1JsU4eG6oIqXz0A",
    "Kj8Hs2LpQ9vXc4Rt7YwB1nMd5FgZa3EoUi6Ty0Vb",
    "q9Zr-X2mK_7vLp4Wc8YtB1nHd5FgJa3EoUi6Sy0AbQe",
    "Tn4Qx8Lm2Pv7Bk9Rz5YwC3dFh1Js6UeGoIqXaZ0AhV_Kj8",
    "9f3a-Zk7Q_x2Lm8Pv4Bn1Rt6Yw5Cd0Fh3Js7UeGoIqXzA",
    "xK9mQ2vLp7WcR4tY8wB1nHd5FgJa3EoUi6SyZ0AbQeTkLz",
)


def test_a_path_of_camel_case_words_is_not_a_secret_but_random_runs_of_every_shape_still_are():
    for text in CAMEL_PATHS:
        assert security.find_secret(text) is None, text
        assert security.redact_secrets(f"see {text}.") == f"see {text}.", text
        security.reject_secrets(url=text)
    for token in RANDOM_SHAPES:
        assert security.find_secret(token) == "looks like a long random token", token
        assert security.find_secret(f"https://x.example.test/{token}") is not None, token


def test_a_secret_is_found_anywhere_in_a_long_text():
    prose = "A line of ordinary words about the work. " * 40
    assert security.find_secret(prose) is None
    assert security.find_secret(prose + KEYS[2][0] + "\n" + prose) is not None
    assert security.find_secret(RANDOM + " " + prose) == "looks like a long random token"


# --- redact_secrets ------------------------------------------------------------------------------------------------


def test_nothing_to_redact_comes_back_as_it_was():
    assert security.redact_secrets("") == "" and security.redact_secrets(None) is None
    plain = "Ran the draft tool: 3 postings, no errors, ab" * 3
    assert security.redact_secrets(plain) == plain


def test_each_kind_of_secret_is_replaced_and_the_words_around_it_stay():
    for key, _ in KEYS:
        assert security.redact_secrets(f"key {key} was printed") == "key [hidden] was printed", key
    assert security.redact_secrets(f"token={JWT};done") == "token=[hidden];done"
    assert security.redact_secrets(f"before\n{PEM}\nafter") == "before\n[hidden]\nafter"
    assert security.redact_secrets(f"id {RANDOM}.") == "id [hidden]."


def test_a_private_key_block_that_never_ends_is_hidden_to_the_end_of_the_text():
    cut = "output: -----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nmore lines"
    assert security.redact_secrets(cut) == "output: [hidden]"


def test_several_secrets_in_one_text_are_all_hidden():
    text = f"a {KEYS[0][0]} b {JWT} c {RANDOM} d"
    assert security.redact_secrets(text) == "a [hidden] b [hidden] c [hidden] d"


# --- scrub ---------------------------------------------------------------------------------------------------------


def test_a_value_under_a_name_that_says_secret_is_removed_whatever_it_holds():
    payload = {
        "name": "Programs Lead", "api_key": "x", "API-KEY": "x", "Authorization": "Bearer abc", "password": "p",
        "db_passwd": "p", "passcode": "1234", "client_secret": "s", "token": "t", "access_token": "t",
        "private_key": "k", "credential": "c", "op://vault/item": "v", "count": 3}
    scrubbed = security.scrub(payload)
    kept = {"name": "Programs Lead", "count": 3}
    assert scrubbed == kept | {k: REMOVED for k in payload if k not in kept}


def test_scrub_goes_into_lists_and_dicts_and_leaves_numbers_and_nothing_alone():
    payload = {"roles": [{"title": "Lead", "secret": "x", "tags": ["a", "b"]}, None, 4, True], "ok": None, "n": 1.5}
    assert security.scrub(payload) == {
        "roles": [{"title": "Lead", "secret": REMOVED, "tags": ["a", "b"]}, None, 4, True], "ok": None, "n": 1.5}
    assert security.scrub(None) is None and security.scrub(7) == 7 and security.scrub([]) == []


def test_a_vault_reference_is_removed_wherever_it_appears_in_a_string_and_a_key_inside_one_is_hidden():
    assert security.scrub("use op://Private/login/password and op://x/y") == "use [removed] and [removed]"
    assert security.scrub({"note": f"the key is {KEYS[2][0]}"}) == {"note": "the key is [hidden]"}
    assert security.scrub(["fine", PEM]) == ["fine", "[hidden]"]


# --- reject_secrets ------------------------------------------------------------------------------------------------


def test_reject_secrets_lets_clean_and_empty_fields_through():
    security.reject_secrets()
    security.reject_secrets(note="a plain note", quote=None, source="", why="because")


def test_reject_secrets_names_the_first_field_that_holds_one_and_asks_to_redact():
    with pytest.raises(SecretRejected) as caught:
        security.reject_secrets(note="fine", quote=f"it said {KEYS[2][0]}", why=JWT)
    assert caught.value.field == "quote" and caught.value.reason.startswith("looks like an API key")
    assert str(caught.value) == f"quote: {caught.value.reason} -- redact and resend"
    assert isinstance(caught.value, DomainError) and KEYS[2][0] not in str(caught.value)


# --- the small helpers around it -----------------------------------------------------------------------------------


def test_a_hash_of_a_token_is_its_sha256_and_never_the_token():
    assert security.hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert security.hash_token("abc") != security.hash_token("abd")


def test_text_over_its_fields_limit_is_refused_with_the_field_and_the_numbers_and_never_cut():
    assert security.cap_length("x" * 2000, "note") == "x" * 2000
    assert security.cap_length("x" * 500, "quote", 500) == "x" * 500
    with pytest.raises(TooLong, match=r"quote is too long \(501 > 500 chars\)"):
        security.cap_length("x" * 501, "quote", 500)
    with pytest.raises(TooLong, match=r"note is too long \(2001 > 2000 chars\)"):
        security.cap_length("x" * 2001, "note")


# --- textutil ------------------------------------------------------------------------------------------------------


def test_squash_folds_every_run_of_whitespace_and_reads_empty_values_as_nothing():
    assert textutil.squash("  a\n\n b\t\tc  ") == "a b c"
    assert textutil.squash(None) == "" and textutil.squash(0) == "" and textutil.squash("") == ""
    assert textutil.squash(12) == "12" and textutil.squash(["a", "b"]) == "['a', 'b']"


def test_a_text_is_cut_between_words_with_an_ellipsis_and_kept_whole_when_it_fits():
    assert textutil.cut_at_word("alpha beta", 10) == "alpha beta"
    assert textutil.cut_at_word("alpha beta gamma delta", 12) == "alpha beta…"
    assert textutil.cut_at_word("alpha beta gamma", 11) == "alpha beta…", "the cut falls on a word's end"
    assert textutil.cut_at_word("alpha, beta; gamma", 13) == "alpha, beta…", "no dangling punctuation"


def test_a_text_with_no_break_to_cut_at_is_cut_in_the_word_and_so_is_one_whose_first_break_comes_too_early():
    assert textutil.cut_at_word("x" * 30, 10) == "x" * 9 + "…"
    assert textutil.cut_at_word("ab " + "c" * 30, 20) == "ab " + "c" * 16 + "…"
    assert textutil.cut_at_word("anything", 0) == "…"


def test_one_line_is_squashed_first_and_then_cut_and_ellipsize_cuts_plainly():
    assert textutil.one_line("a   b\n c\td", 100) == "a b c d" and textutil.one_line(None, 5) == ""
    assert textutil.one_line("alpha\nbeta\ngamma", 12) == "alpha beta…"
    assert textutil.ellipsize("abcdef", 4) == "abc…" and textutil.ellipsize("ab cdef", 4) == "ab…"
    assert textutil.ellipsize("abcd", 4) == "abcd" and textutil.ellipsize("", 4) == ""


def test_clip_trims_cuts_without_an_ellipsis_keeps_line_breaks_and_says_none_for_nothing():
    assert textutil.clip(None, 5) is None and textutil.clip("   \n", 5) is None
    assert textutil.clip("  abcdef ", 3) == "abc" and textutil.clip("a\nb", 10) == "a\nb"


def test_the_first_letter_is_raised_or_lowered_and_an_acronym_can_keep_its_capitals():
    assert textutil.upper_first("élan vital") == "Élan vital" and textutil.upper_first("") == ""
    assert textutil.lower_first("Reads the board") == "reads the board" and textutil.lower_first("") == ""
    assert textutil.lower_first("DNS records") == "dNS records"
    assert textutil.lower_first("DNS records", keep_acronym=True) == "DNS records"
    assert textutil.lower_first("Dns records", keep_acronym=True) == "dns records"
    assert textutil.lower_first("A", keep_acronym=True) == "a"
