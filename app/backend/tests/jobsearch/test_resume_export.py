"""The master resume exported as a JSON Resume file (docs/PRD.md 8.4; the owner, 2026-10-30): an exportable format that
another tool can import. `resume_export.json_resume` turns the master (the resume and
about documents) into a JSON Resume v1.0.0 document, and two routes hand it out as `resume.json`: the version in force
and an earlier one. The document is checked against the schema as published (`tests/jsonresume/`), what it leaves out is
pinned as well as what it holds, and an export reads the master and changes nothing. Made-up data only."""

import json
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from sqlalchemy import select

from app import db
from app.jobsearch.models import ProfileVersion
from app.jobsearch.profile.documents import About, Resume
from app.jobsearch.resume import export as resume_export
from app.timeutil import aware
from tests.builders import ABOUT, RESUME
from tests.support import OWNER

SCHEMA = json.loads((db.BACKEND_DIR / "tests" / "jsonresume" / "schema.json").read_text(encoding="utf-8"))
DATE = re.compile(SCHEMA["definitions"]["iso8601"]["pattern"])
NOW = datetime(2026, 10, 30, 14, 5, 9, 123456, tzinfo=UTC)
#: The schema the file names, as JSON Resume publishes it for the version the owner named.
SCHEMA_URL = "https://raw.githubusercontent.com/jsonresume/resume-schema/v1.0.0/schema.json"


def problems(doc) -> list[str]:
    """What the published schema says is wrong with `doc`, as the schema's own checker finds it (formats included)."""
    checker = validator_for(SCHEMA)(SCHEMA, format_checker=FormatChecker())
    return [f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in checker.iter_errors(doc)]


def export(resume: dict, about: dict | None = None, *, master=1, about_version=1, at=NOW) -> dict:
    return resume_export.json_resume(
        Resume.model_validate(resume), About.model_validate(about or {}),
        master_version=master, about_version=about_version, modified_at=at)


# --- a made-up master with every section filled, and a word in everything the export must leave out ------------------

#: Every id carries `zq-`, so one search shows that no id reaches the file; every word that stays out carries SENTINEL.
RICH = {
    "summary": "Builds programs that people use. Writes plainly.",
    "summary_variants": [
        {"id": "zq-sv", "text": "SENTINEL-VARIANT a shorter summary", "note": "SENTINEL-VARIANT-NOTE"}],
    "achievements": [
        {"id": "zq-ach", "text": "SENTINEL-ACHIEVEMENT led a statewide program", "note": "SENTINEL-ACH-NOTE"}],
    "philosophy": "SENTINEL-PHILOSOPHY people first",
    "competencies": [
        {"id": "zq-gov", "area": "Governance", "items": ["Risk Frameworks", "Policy Design"]},
        {"id": "zq-del", "area": "Delivery", "items": ["Roadmapping"]},
    ],
    "roles": [
        {
            "id": "zq-now", "employer": "Northwind Labs", "location": "Oakfield, ST", "title": "Director of Programs",
            "start": "Jan 2021", "end": "Present", "intro": "Runs the programs practice.",
            "descriptor": "regional logistics company", "optional": False, "note": "SENTINEL-ROLE-NOTE",
            "tools": ["SENTINEL-TOOL"],
            "bullets": [
                {"id": "zq-b1", "text": "Led the risk working group", "note": "SENTINEL-BULLET-NOTE",
                 "group": "SENTINEL-GROUP"},
                {"id": "zq-b2", "text": "Built the practice knowledge base", "note": "", "group": ""},
            ],
            "evidence": [{"id": "zq-ev", "title": "SENTINEL-EVIDENCE-TITLE", "text": "SENTINEL-EVIDENCE-TEXT"}],
        },
        {
            "id": "zq-prev", "employer": "Northwind Labs", "location": "Oakfield, ST", "title": "Senior Manager",
            "start": "03/2016", "end": "2020-12-31", "intro": "", "descriptor": "", "optional": False, "note": "",
            "tools": [], "bullets": [{"id": "zq-b3", "text": "Ran the client engagements", "note": "", "group": ""}],
            "evidence": [],
        },
        {
            "id": "zq-eng", "employer": "Fabrikam", "location": "", "title": "Engineer", "start": "2012", "end": "2015",
            "intro": "", "descriptor": "", "optional": True, "note": "", "tools": [], "bullets": [], "evidence": [],
        },
        {
            "id": "zq-intern", "employer": "Contoso", "location": "", "title": "Intern", "start": "early days",
            "end": "2004 - 2006", "intro": "", "descriptor": "", "optional": False, "note": "", "tools": [],
            "bullets": [{"id": "zq-b4", "text": "Filed the paperwork", "note": "", "group": ""}], "evidence": [],
        },
    ],
    "education": [
        {"id": "zq-ms", "degree": "MS in Leadership", "school": "Example University", "note": "SENTINEL-EDU-NOTE"},
        {"id": "zq-ba", "degree": "BA", "school": "", "note": ""},
    ],
    "projects": [
        {"id": "zq-pr", "text": "Built a volunteer scheduling tool for a food bank.", "note": "SENTINEL-PROJECT-NOTE"}],
    "awards": [{"id": "zq-aw", "text": "Employee of the Year, 2019", "note": "SENTINEL-AWARD-NOTE"}],
    "notes": [{"id": "zq-note", "text": "SENTINEL-RESUME-NOTE", "source": "TODO"}],
}
RICH_ABOUT = {
    "name": "Pat Example", "location": "Oakfield, ST", "metro_location": "SENTINEL-METRO", "address": "12 Example Lane",
    "phone": "555-010-0199", "email": "pat@example.test",
    "links": ["https://www.boardone.test/in/pat-example", "pat-example.example.test/portfolio", "not a link"],
    "work_authorization": "SENTINEL-AUTHORIZATION", "sponsorship": "SENTINEL-SPONSORSHIP",
    "current_company": "SENTINEL-COMPANY", "notes": [{"id": "zq-an", "text": "SENTINEL-ABOUT-NOTE", "source": "old"}],
}
#: A master that holds nothing but a name and one line: the blank email and every blank date are left out, not written.
BARE = {"summary": "Builds programs."}


def rich() -> dict:
    return export(RICH, RICH_ABOUT, master=3, about_version=2)


# --- dates ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("", None), ("   ", None),
    # A bare year stays a year: no month is borrowed.
    ("2019", "2019"), ("Class of 2019", "2019"), ("2019 (Q1)", "2019"),
    # A full date, kept when it is a real day.
    ("2019-03-15", "2019-03-15"), ("2020-02-29", "2020-02-29"), ("2019-02-30", "2019"), ("2019-00-10", "2019"),
    # A month and a year, in the forms a resume writes them.
    ("2019-03", "2019-03"), ("2019-3", "2019-03"), ("03/2019", "2019-03"), ("3-2019", "2019-03"),
    ("03.2019", "2019-03"), ("2019/03", "2019-03"), ("2019-12", "2019-12"),
    ("Jan 2019", "2019-01"), ("January 2019", "2019-01"), ("Sept. 2019", "2019-09"), ("sep 2019", "2019-09"),
    ("MAY 2019", "2019-05"), ("December, 2019", "2019-12"), ("Dec 2019", "2019-12"),
    # The year is certain and the rest is not.
    ("Spring 2019", "2019"), ("Q1 2019", "2019"), ("03/04/2019", "2019"), ("Summer of 2019", "2019"),
    ("2019-13", "2019"), ("13/2019", "2019"), ("00/2019", "2019"), ("Marketing intern, 2019", "2019"),
    ("Jan - Mar 2019", "2019"),
    # A number after the year that is a length of time, or a quarter, or the tail of a school year, is no month.
    ("2019 - 2 years", "2019"), ("2019 / 2 yrs", "2019"), ("2019 - 6 months contract", "2019"), ("2019-6 mo", "2019"),
    ("Q1-2019", "2019"), ("H1/2019", "2019"), ("2018-19", "2018"), ("2019-03 Acme", "2019"),
    # ... while a month with something else after it is still a month.
    ("Mar 2019 (6 mo)", "2019-03"), ("2019-03 (contract)", "2019-03"), ("03/2019 (6 mo)", "2019-03"),
    ("Jan\u00a02019", "2019-01"),
    # Only the letters and digits of the date are read: a long s is no "s", and an Arabic-Indic digit is no digit.
    ("\u017fep 2019", "2019"), ("\u017feptember 2019", "2019"), ("20\u0661\u0669", None),
    # No year, or more than one: no guess.
    ("early days", None), ("Present", None), ("2019 - 2022", None), ("Jan 2019 - Mar 2022", None),
    ("1899", None), ("2101", None), ("a while after", None),
])
def test_a_start_date_is_read_only_as_far_as_it_is_certain(text, expected):
    assert resume_export.iso_date(text) == expected
    if expected is not None:
        assert DATE.fullmatch(expected)


@pytest.mark.parametrize(
    "text", ["Present", "present", "Current", "now", "Today", "ongoing", "To date", "2019 - Present"])
def test_an_end_that_says_it_goes_on_has_no_end_date(text):
    assert resume_export.iso_date(text, end=True) is None


@pytest.mark.parametrize("text, expected", [
    ("", None), ("2022", "2022"), ("Dec 2022", "2022-12"), ("12/2022", "2022-12"), ("2022-06-30", "2022-06-30"),
    ("2019 - 2022", None), ("sometime later", None),
    # A school year ends in the second year, so its first year is not the end.
    ("2018-19", None), ("2018/19", None), ("2018-19 school year", None),
])
def test_an_end_that_is_a_date_is_read_as_a_start_is(text, expected):
    assert resume_export.iso_date(text, end=True) == expected


# --- the published schema ---------------------------------------------------------------------------------------------


def test_the_schema_the_export_is_checked_against_is_the_published_one_and_refuses_what_the_export_avoids():
    assert SCHEMA["$schema"] == "http://json-schema.org/draft-04/schema#"
    assert len(SCHEMA["properties"]) == 14
    # A passing check means something: the schema refuses an empty email, a date in words and a key it has no place for.
    assert problems({"basics": {"email": ""}})
    assert problems({"work": [{"startDate": "Jan 2019"}]})
    assert problems({"achievements": []})
    assert problems({"basics": {"name": "Pat Example"}, "meta": {"lastModified": "2026-10-30T14:05:09"}}) == []


#: A master whose free text is what the standard's formats and date pattern refuse: an email that is no address, links
#: that are no web addresses, dates written with letters and digits of other scripts. None of it is in the file, and the
#: rest of the file is still valid.
HOSTILE = {"summary": "Builds programs.", "roles": [
    {"id": "zq-h1", "employer": "Acme", "title": "Lead", "start": "\u017fep 2019", "end": "20\u0661\u0669"},
    {"id": "zq-h2", "employer": "Initech", "title": "Lead", "start": "2018 - 2 years", "end": "Dec '21"}]}
HOSTILE_ABOUT = {"name": "Pat Example", "email": "n/a", "links": [
    "https://example.test/a|b", "https://example.test:99999/x", "https://ex\u00e4mple.test/\u00fc"]}


@pytest.mark.parametrize("name, resume, about", [
    ("the shared made-up master", RESUME, ABOUT),
    ("a master with every field filled", RICH, RICH_ABOUT),
    ("a bare master with a blank email and no dates", BARE, {"name": "Pat Example", "email": ""}),
    ("a master whose free text the standard would refuse", HOSTILE, HOSTILE_ABOUT),
])
def test_the_export_is_a_valid_json_resume_document(name, resume, about):
    doc = export(resume, about)
    assert problems(doc) == [], name
    assert doc["$schema"] == SCHEMA_URL
    # The schema checker here does not enforce `format: uri` and takes any text with an "@" for an email, so each is
    # checked to be what it says (`test_an_email_is_written_only_when_it_reads_as_one_address`, and the links').
    urls = [p["url"] for p in doc.get("basics", {}).get("profiles", [])]
    assert all(u.startswith(("http://", "https://")) for u in urls)


def test_free_text_the_standard_would_refuse_is_left_out_and_the_rest_of_the_master_is_still_written():
    doc = export(HOSTILE, HOSTILE_ABOUT)
    assert doc["basics"] == {"name": "Pat Example", "summary": "Builds programs."}
    assert doc["work"] == [{"name": "Acme", "position": "Lead"}, {"name": "Initech", "position": "Lead"}]


# --- what is in it ----------------------------------------------------------------------------------------------------


def test_the_basics_come_from_the_facts_in_force_and_the_summary():
    assert rich()["basics"] == {
        "name": "Pat Example", "email": "pat@example.test", "phone": "555-010-0199",
        "summary": "Builds programs that people use. Writes plainly.",
        "location": {"address": "12 Example Lane", "city": "Oakfield, ST"},
        "profiles": [
            {"network": "boardone.test", "url": "https://www.boardone.test/in/pat-example"},
            {"network": "pat-example.example.test", "url": "https://pat-example.example.test/portfolio"},
        ],
    }


@pytest.mark.parametrize("where, location", [
    ("Oakfield, ST", {"city": "Oakfield, ST"}),
    ("Oakfield,ST", {"city": "Oakfield,ST"}),
    ("London, UK", {"city": "London, UK"}),
    ("Paris, FR", {"city": "Paris, FR"}),
    ("Oakfield, Exampleland", {"city": "Oakfield, Exampleland"}),
    ("Oakfield, ST, Exampleland", {"city": "Oakfield, ST, Exampleland"}),
    ("Oakfield", {"city": "Oakfield"}),
    ("", None),
])
def test_a_place_is_the_whole_text_as_the_city_and_is_never_split_into_a_region(where, location):
    # Two capital letters after a comma are a state in one country and a country in another ("DE", "CA", "UK"):
    # the text is not taken apart, so no country is ever written as a region.
    basics = export(BARE, {"name": "Pat Example", "location": where})["basics"]
    assert basics.get("location") == location


@pytest.mark.parametrize("link, shown", [
    ("https://example.test/in/pat", ("example.test", "https://example.test/in/pat")),
    ("http://www.example.test/pat", ("example.test", "http://www.example.test/pat")),
    ("HTTPS://Example.Test/Pat", ("example.test", "HTTPS://Example.Test/Pat")),
    ("example.test/in/pat", ("example.test", "https://example.test/in/pat")),
    ("www.example.test", ("example.test", "https://www.example.test")),
    ("pat.example.test:8080/x?y=1#z", ("pat.example.test", "https://pat.example.test:8080/x?y=1#z")),
    ("  https://example.test/pat  ", ("example.test", "https://example.test/pat")),
    ("not a link", None), ("pat", None), ("pat@example.test", None), ("mailto:pat@example.test", None),
    ("ftp://example.test/pat", None), ("javascript://example.test/%0Aalert(1)", None), ("https://", None), ("", None),
    ("my page, example.test/pat", None),
    # Not a URI as RFC 3986 writes one, so a strict reader of `format: uri` would refuse the file.
    ("https://example.test/a|b", None), ("https://example.test:99999/x", None), ("https://example.test:80a/x", None),
    ("https://ex\u00e4mple.test/\u00fc", None), ("https://example.test/\u00fc", None),
    ("https://example.test" + chr(92) + "x", None), ("https://example.test/a b", None),
    ("https://example.test/%zz", None),
    ("https://example.test/{x}", None), ("https://user@example.test/pat", None), ("example.test/a|b", None),
    ("https://example.test/a%20b?q=%41&r=x+y#top", ("example.test", "https://example.test/a%20b?q=%41&r=x+y#top")),
    ("https://example.test:65535/x", ("example.test", "https://example.test:65535/x")),
    ("https://example.test/(a),b;c=d:e@f", ("example.test", "https://example.test/(a),b;c=d:e@f")),
])
def test_a_link_is_a_profile_only_when_it_reads_as_a_web_address(link, shown):
    profiles = export(BARE, {"name": "Pat Example", "links": [link]})["basics"].get("profiles")
    assert profiles == (None if shown is None else [{"network": shown[0], "url": shown[1]}])


@pytest.mark.parametrize("email, written", [
    ("pat@example.test", "pat@example.test"), ("pat.q+jobs@mail.example.test", "pat.q+jobs@mail.example.test"),
    ("o'neil@example.test", "o'neil@example.test"),
    ("pat_1-x@sub-domain.example.test", "pat_1-x@sub-domain.example.test"),
    # Spaces round it are not part of the address, as they are not part of a link.
    (" pat@example.test ", "pat@example.test"), ("pat@example.test\n", "pat@example.test"),
    ("", None), ("   ", None), ("n/a", None), ("none", None), ("pat@", None), ("@example.test", None),
    ("pat@example", None), ("pat@@example.test", None), ("pat@example..test", None), ("pat@.example.test", None),
    (".pat@example.test", None), ("pat.@example.test", None), ("pat..x@example.test", None),
    ("pat@-example.test", None), ("pat@example.t", None), ("pat@example.123", None),
    ("pat example@example.test", None), ("pat@example.test, pat2@example.test", None),
    ("pat@example.test; pat2@example.test", None), ("Pat <pat@example.test>", None), ("<pat@example.test>", None),
    ("mailto:pat@example.test", None), ("pat@ex\u00e4mple.test", None), ("p\u00e4t@example.test", None),
    ("pat@[192.0.2.1]", None), ("pat@example.test.", None),
])
def test_an_email_is_written_only_when_it_reads_as_one_address(email, written):
    # The standard calls it `format: email`, and an importer (Reactive Resume) refuses the whole file for one that is
    # not: so a note, a list or a name with brackets is left out rather than copied.
    basics = export(BARE, {"name": "Pat Example", "email": email})["basics"]
    assert basics.get("email") == written


def test_each_role_is_one_work_entry_in_the_masters_order_optional_roles_too_with_the_dates_it_can_read():
    assert rich()["work"] == [
        {
            "name": "Northwind Labs", "position": "Director of Programs", "location": "Oakfield, ST",
            "description": "regional logistics company", "summary": "Runs the programs practice.",
            "startDate": "2021-01",
            "highlights": ["Led the risk working group", "Built the practice knowledge base"],
        },
        {
            "name": "Northwind Labs", "position": "Senior Manager", "location": "Oakfield, ST",
            "startDate": "2016-03", "endDate": "2020-12-31", "highlights": ["Ran the client engagements"],
        },
        {"name": "Fabrikam", "position": "Engineer", "startDate": "2012", "endDate": "2015"},
        {"name": "Contoso", "position": "Intern", "highlights": ["Filed the paperwork"]},
    ]


@pytest.mark.parametrize("start, end, dates", [
    ("Jan 2018", "Dec 2021", {"startDate": "2018-01", "endDate": "2021-12"}),
    # A job that goes on has a start and no end; so has one with no end written, which the file cannot say otherwise.
    ("Jan 2018", "Present", {"startDate": "2018-01"}), ("Jan 2018", "to date", {"startDate": "2018-01"}),
    ("Jan 2018", "", {"startDate": "2018-01"}), ("Jan 2018", "  ", {"startDate": "2018-01"}),
    # An end that is written but cannot be read for certain is no "goes on": a file with a start and no end says the job
    # is still held (importers print "Present"), so no start is written either.
    ("Jan 2018", "Dec '21", {}), ("Jan 2018", "12/23", {}), ("Jan 2018", "Dec 23", {}), ("Jan 2018", "TBD", {}),
    ("Jan 2018", "last year", {}), ("Jan 2018", "Mar 2015 - Jun 2016", {}), ("Jan 2018", "2018-19", {}),
    ("Jan 2018", "20\u0661\u0669", {}),
    # A start that cannot be read leaves out only itself.
    ("early days", "2015", {"endDate": "2015"}), ("early days", "", {}), ("early days", "Present", {}),
])
def test_an_end_that_is_written_but_cannot_be_read_takes_the_start_with_it_so_no_job_is_said_to_go_on(
        start, end, dates):
    resume = {"roles": [{"id": "zq-r", "employer": "Fabrikam", "title": "Engineer", "start": start, "end": end}]}
    assert export(resume)["work"] == [{"name": "Fabrikam", "position": "Engineer", **dates}]


def test_a_degree_is_one_education_entry_written_whole_and_with_no_dates():
    assert rich()["education"] == [
        {"institution": "Example University", "studyType": "MS in Leadership"}, {"studyType": "BA"}]


def test_each_competency_is_a_skill_with_its_terms_as_keywords():
    assert rich()["skills"] == [
        {"name": "Governance", "keywords": ["Risk Frameworks", "Policy Design"]},
        {"name": "Delivery", "keywords": ["Roadmapping"]},
    ]


def test_an_award_is_a_title_and_a_project_a_name_each_the_masters_whole_line():
    # `name` is the one field every importer keeps a project by (Reactive Resume drops a project that has none), and the
    # master has one line for it: written as the description, the whole section would be lost in that tool.
    doc = rich()
    assert doc["awards"] == [{"title": "Employee of the Year, 2019"}]
    assert doc["projects"] == [{"name": "Built a volunteer scheduling tool for a food bank."}]


def test_the_sections_come_in_the_standards_order():
    assert list(rich()) == ["$schema", "basics", "work", "education", "awards", "skills", "projects", "meta"]


def test_what_the_master_has_nothing_of_is_left_out_rather_than_written_empty():
    assert export(BARE, {"name": "Pat Example"}, master=1, about_version=1) == {
        "$schema": SCHEMA_URL,
        "basics": {"name": "Pat Example", "summary": "Builds programs."},
        "meta": {"lastModified": "2026-10-30T14:05:09", "resumebot": {"masterVersion": 1, "aboutVersion": 1}},
    }


def test_a_line_or_role_with_nothing_in_it_is_not_an_entry():
    resume = {
        **BARE, "roles": [{"id": "zq-empty", "employer": "", "title": ""}], "awards": [{"id": "zq-a", "text": " "}],
        "projects": [{"id": "zq-p", "text": ""}], "competencies": [{"id": "zq-c", "area": "", "items": [""]}],
        "education": [{"id": "zq-e", "degree": "", "school": ""}]}
    assert set(export(resume)) == {"$schema", "basics", "meta"}


def test_a_role_with_only_dates_is_not_an_entry():
    resume = {**BARE, "roles": [{"id": "zq-r", "employer": "", "title": "", "start": "2019", "end": "2020"}]}
    assert "work" not in export(resume)


def test_the_texts_are_copied_exactly_as_the_master_has_them():
    resume = {
        **BARE, "summary": "  Builds   programs,\nwith  care.  ", "awards": [{"id": "zq-a", "text": " Best  of 2019 "}],
        "roles": [{"id": "zq-r", "employer": " Northwind  Labs ", "title": "Lead ", "bullets": [
            {"id": "zq-b", "text": "  Led the  group.  "}]}]}
    doc = export(resume, {"name": " Pat  Example ", "phone": "(555) 010 0199 "})
    assert doc["basics"]["summary"] == "  Builds   programs,\nwith  care.  "
    assert doc["basics"]["name"] == " Pat  Example " and doc["basics"]["phone"] == "(555) 010 0199 "
    assert doc["awards"] == [{"title": " Best  of 2019 "}]
    assert doc["work"] == [{"name": " Northwind  Labs ", "position": "Lead ", "highlights": ["  Led the  group.  "]}]


def test_every_master_text_in_scope_is_in_the_file():
    text = json.dumps(rich(), ensure_ascii=False)
    shown = [
        "Pat Example", "pat@example.test", "555-010-0199", "12 Example Lane",
        "Builds programs that people use. Writes plainly.",
        "Northwind Labs", "Director of Programs", "Senior Manager", "Oakfield, ST", "regional logistics company",
        "Runs the programs practice.", "Led the risk working group", "Built the practice knowledge base",
        "Ran the client engagements", "Fabrikam", "Engineer", "Contoso", "Intern", "Filed the paperwork",
        "MS in Leadership", "Example University", "BA", "Governance", "Risk Frameworks", "Policy Design", "Delivery",
        "Roadmapping", "Employee of the Year, 2019", "Built a volunteer scheduling tool for a food bank.",
    ]
    assert [w for w in shown if json.dumps(w, ensure_ascii=False)[1:-1] not in text] == []


def test_what_the_standard_has_no_field_for_is_left_out_and_no_internal_id_reaches_the_file():
    text = json.dumps(rich(), ensure_ascii=False)
    assert re.findall(r"SENTINEL-[A-Z-]+", text) == []
    assert "zq-" not in text
    # The metro location, work authorization, sponsorship and current company are forms' data, not a resume's.
    for stays_out in ("achievement", "philosophy", "tools", "evidence", "note", "optional", "metro", "sponsor"):
        assert stays_out not in text.lower(), stays_out


def test_a_role_marked_optional_or_noted_is_written_like_any_other():
    plain = {"id": "zq-r", "employer": "Fabrikam", "title": "Engineer", "start": "2012", "end": "2015"}
    kept = {**plain, "optional": True, "note": "SENTINEL-ROLE-NOTE", "tools": ["SENTINEL-TOOL"]}
    assert export({"roles": [plain]})["work"] == export({"roles": [kept]})["work"]


def test_the_meta_names_when_it_was_last_saved_and_the_versions_it_was_made_from():
    assert rich()["meta"] == {
        "lastModified": "2026-10-30T14:05:09", "resumebot": {"masterVersion": 3, "aboutVersion": 2}}


def test_the_time_is_written_in_utc_whatever_zone_the_clock_gave():
    at = datetime(2026, 10, 30, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert export(BARE, at=at)["meta"]["lastModified"] == "2026-10-31T04:30:00"
    assert export(BARE, at=datetime(2026, 10, 30, 1, 2, 3))["meta"]["lastModified"] == "2026-10-30T01:02:03"


@pytest.mark.parametrize("resume, empty", [
    ({}, True), ({"summary": "  "}, True), ({"achievements": [{"id": "zq-a", "text": "Led it"}]}, True),
    ({"summary": "Builds programs."}, False), ({"roles": [{"id": "zq-r", "employer": "X", "title": "Y"}]}, False),
    # A role with nothing in it is no role: the file would hold nothing, so there is nothing to export.
    ({"roles": [{"id": "zq-r", "employer": "", "title": ""}]}, True),
    ({"roles": [{"id": "zq-r", "employer": " ", "title": "", "intro": " ", "bullets": [{"id": "zq-b", "text": " "}]}]},
     True),
    ({"roles": [{"id": "zq-r", "employer": "", "title": "", "start": "2019", "end": "2020"}]}, True),
    ({"roles": [{"id": "zq-r", "employer": "", "title": "", "bullets": [{"id": "zq-b", "text": "Led it"}]}]}, False),
    ({"roles": [{"id": "zq-r", "employer": "", "title": "", "intro": "Ran it."}]}, False),
    ({"roles": [{"id": "zq-r", "employer": "", "title": ""}, {"id": "zq-s", "employer": "X", "title": ""}]}, False),
])
def test_a_master_with_no_summary_and_no_role_has_nothing_to_export(resume, empty):
    assert resume_export.nothing_to_export(Resume.model_validate(resume)) is empty


# --- the routes -------------------------------------------------------------------------------------------------------

URL = "/api/v1/documents/resume/export.json"


def version_url(n) -> str:
    return f"/api/v1/documents/resume/versions/{n}/export.json"


async def _save(client, kind: str, data: dict, why: str = "") -> int:
    put = await client.put(f"/api/v1/profile/{kind}", json={"data": data, "why": why})
    assert put.status_code == 200, put.text
    return put.json()["version"]


def bullet_resume(text: str) -> dict:
    return {**RESUME, "roles": [{**RESUME["roles"][0], "bullets": [{"id": "b-one", "text": text, "note": ""}]}]}


def bullets(doc: dict) -> list[str]:
    return [h for w in doc["work"] for h in w.get("highlights", [])]


async def _when(kind: str, number: int) -> datetime:
    """When version `number` of `kind` was saved, as the store has it."""
    async with db.session_factory()() as session:
        return aware(await session.scalar(select(ProfileVersion.saved_at).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == kind, ProfileVersion.version == number)))


async def _put(kind: str, number: int, data: dict, saved_at: datetime) -> None:
    """A version saved at a time of the test's choosing (a save through the API is saved now)."""
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind=kind, version=number, data=data, saved_by="test", why="",
                                   saved_at=saved_at))
        await session.commit()


async def test_the_current_version_downloads_as_resume_json_with_the_master_as_json_resume(client):
    await _save(client, "resume", RICH)
    await _save(client, "about", RICH_ABOUT)
    got = await client.get(URL)
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/json"
    assert got.headers["content-disposition"] == 'attachment; filename="resume.json"'
    assert got.content.endswith(b"}\n")
    saved = max(await _when("resume", 1), await _when("about", 1))
    assert got.json() == export(RICH, RICH_ABOUT, master=1, about_version=1, at=saved)
    assert problems(got.json()) == []


async def test_the_file_is_utf_8_and_readable_with_indentation(client):
    await _save(client, "resume", {**BARE, "summary": "Zoë builds programs for naïve users"})
    await _save(client, "about", {"name": "Zoë Example"})
    got = await client.get(URL)
    assert "Zoë builds programs for naïve users".encode() in got.content
    assert b"\\u00" not in got.content
    assert got.content.startswith(b'{\n  "$schema"')


async def test_an_export_changes_nothing_in_the_master(client):
    await _save(client, "resume", RICH, "first")
    await _save(client, "about", RICH_ABOUT, "first")

    async def stored() -> list[tuple]:
        async with db.session_factory()() as session:
            rows = (await session.scalars(
                select(ProfileVersion).order_by(ProfileVersion.kind, ProfileVersion.version))).all()
            return [(r.kind, r.version, r.data, r.saved_by, r.why, r.saved_at) for r in rows]

    before, shown = await stored(), (await client.get("/api/v1/documents")).json()
    for url in (URL, version_url(1)):
        assert (await client.get(url)).status_code == 200
    assert await stored() == before
    assert (await client.get("/api/v1/documents")).json() == shown
    assert (await client.get("/api/v1/profile/resume/history")).json()["versions"][0]["why"] == "first"


async def test_an_earlier_version_exports_its_own_content_and_the_current_route_the_newest(client):
    assert await _save(client, "resume", bullet_resume("Bullet A, from the first version")) == 1
    assert await _save(client, "resume", bullet_resume("Bullet B, from the second version")) == 2
    first = (await client.get(version_url(1))).json()
    second = (await client.get(version_url(2))).json()
    current = (await client.get(URL)).json()
    assert bullets(first) == ["Bullet A, from the first version"]
    assert bullets(second) == ["Bullet B, from the second version"]
    assert bullets(current) == ["Bullet B, from the second version"]
    assert [d["meta"]["resumebot"]["masterVersion"] for d in (first, second, current)] == [1, 2, 2]
    assert (await client.get(version_url(1))).headers["content-disposition"] == 'attachment; filename="resume.json"'


async def test_an_earlier_version_carries_the_contact_details_in_force_now_and_says_which_version_of_them(client):
    await _save(client, "resume", BARE)
    await _save(client, "about", {"name": "Pat Example", "phone": "555-010-0100"})
    await _save(client, "resume", {**BARE, "summary": "Builds programs, later."})
    await _save(client, "about", {"name": "Pat Example", "phone": "555-010-0222"})
    first = (await client.get(version_url(1))).json()
    assert first["basics"]["phone"] == "555-010-0222"
    assert first["basics"]["summary"] == "Builds programs."
    assert first["meta"]["resumebot"] == {"masterVersion": 1, "aboutVersion": 2}


async def test_the_time_in_the_file_is_when_that_version_was_saved_not_when_it_was_exported(client):
    await _put("resume", 1, BARE, datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC))
    await _put("resume", 2, {**BARE, "summary": "Builds programs, later."}, datetime(2026, 3, 1, 9, 15, 30, tzinfo=UTC))
    first, second, current = [await client.get(u) for u in (version_url(1), version_url(2), URL)]
    assert first.json()["meta"]["lastModified"] == "2026-01-05T10:00:00"
    assert second.json()["meta"]["lastModified"] == "2026-03-01T09:15:30"
    assert current.json()["meta"]["lastModified"] == "2026-03-01T09:15:30"
    # So an export of the same version is the same file whenever it is made, and a tool can tell which is newer.
    assert (await client.get(version_url(1))).content == first.content


async def test_the_time_in_the_file_is_when_the_newest_thing_in_it_was_saved_the_contact_details_in_force_too(client):
    await _put("resume", 1, BARE, datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC))
    await _put("about", 1, {"name": "Pat Example"}, datetime(2026, 2, 1, 8, 30, 0, tzinfo=UTC))
    await _put("resume", 2, {**BARE, "summary": "Builds programs, later."}, datetime(2026, 3, 1, 9, 15, 30, tzinfo=UTC))
    await _put("about", 2, {"name": "Pat Example", "phone": "555-010-0222"}, datetime(2026, 2, 20, 7, 0, 0, tzinfo=UTC))
    # Version 1 of the resume is carried with the contact details of 20 February; version 2 is newer than they are.
    assert (await client.get(version_url(1))).json()["meta"]["lastModified"] == "2026-02-20T07:00:00"
    assert (await client.get(version_url(2))).json()["meta"]["lastModified"] == "2026-03-01T09:15:30"


async def test_a_master_without_contact_details_still_exports(client):
    await _save(client, "resume", BARE)
    doc = (await client.get(URL)).json()
    assert doc["basics"] == {"summary": "Builds programs."}
    assert doc["meta"]["resumebot"] == {"masterVersion": 1, "aboutVersion": 0}
    assert problems(doc) == []


@pytest.mark.parametrize("number", [0, 3, 99, -1, 2_147_483_648, 99_999_999_999_999])
async def test_a_version_that_is_not_there_is_a_404_in_words(client, number):
    await _save(client, "resume", BARE)
    await _save(client, "resume", {**BARE, "summary": "Builds more programs."})
    got = await client.get(version_url(number))
    assert got.status_code == 404
    assert got.json()["detail"] == f"there is no version {number} of the resume"


async def test_a_version_of_another_owner_is_not_there(client):
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner="someone-else", kind="resume", version=1, data=BARE, saved_by="test", why=""))
        await session.commit()
    assert (await client.get(version_url(1))).status_code == 404
    assert (await client.get(URL)).status_code == 409


EMPTY = "Your master resume is empty, so there is nothing to export yet."


async def test_a_master_that_was_never_saved_is_a_409_in_words(client):
    got = await client.get(URL)
    assert got.status_code == 409
    assert got.json()["detail"] == EMPTY


@pytest.mark.parametrize("resume", [
    {"philosophy": "People first."}, {"summary": "   "},
    {"achievements": [{"id": "a-one", "text": "Led it", "note": ""}]},
    {"roles": [{"id": "r-blank", "employer": "", "title": ""}]}])
async def test_a_saved_master_with_no_summary_and_no_role_is_a_409_for_every_version_of_it(client, resume):
    await _save(client, "about", {"name": "Pat Example"})
    await _save(client, "resume", resume)
    for url in (URL, version_url(1)):
        got = await client.get(url)
        assert got.status_code == 409 and got.json()["detail"] == EMPTY


async def test_a_stored_version_that_no_longer_fits_is_a_422_in_words_and_the_others_still_export(client):
    await _save(client, "resume", BARE)
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind="resume", version=2, data={"summary": "x", "colour": "blue"},
                                   saved_by="test", why=""))
        await session.commit()
    for url in (URL, version_url(2)):
        got = await client.get(url)
        assert got.status_code == 422
        assert got.json()["detail"].startswith("the resume don't fit: colour:")
    assert (await client.get(version_url(1))).status_code == 200


async def test_contact_details_that_no_longer_fit_are_a_422_in_words(client):
    await _save(client, "resume", BARE)
    async with db.session_factory()() as session:
        session.add(ProfileVersion(owner=OWNER, kind="about", version=1, data={"name": "Pat Example", "colour": "blue"},
                                   saved_by="test", why=""))
        await session.commit()
    got = await client.get(URL)
    assert got.status_code == 422
    assert got.json()["detail"].startswith("the about don't fit: colour:")
