"""What a runbook says of its site's addresses (the generic-sites design, Part 1.2): the rules of `address_rules`, and
the judgements that read them (`address.is_single_posting`, `address.posting_page`, `ats.parse` through `DataSystem`).
Every site is made up (`example.test`); what the real runbooks say is the restricted tier's frozen reference."""

import json

import pytest

from app.jobsearch import address
from app.jobsearch.adapters import Address
from app.jobsearch.adapters.unread import DataSystem
from app.sites import address_rules, loader, registry


def world(tmp_path, *docs: dict):
    shipped = tmp_path / f"shipped{len(list(tmp_path.iterdir()))}"  # one folder for each registry a test makes
    shipped.mkdir()
    for doc in docs:
        (shipped / f"{doc['key']}.json").write_text(json.dumps({"schema": 1, "name": doc["key"].title(), **doc}),
                                                    encoding="utf-8")
    return loader.load(shipped, tmp_path / "absent", ())


BOARD = {"key": "board_x", "kind": "board", "hosts": ["board-x.example.test"],
         "address": {"one_posting": [{"path": "post/[0-9]+"}, {"path": "(?:.+/)?view", "query": {"id": "digits"}}]}}
NO_RULES = {"key": "board_n", "kind": "board", "hosts": ["board-n.example.test"]}
NONE_IS_ONE = {"key": "board_e", "kind": "board", "hosts": ["board-e.example.test"], "address": {"one_posting": []}}


def test_a_board_says_which_of_its_links_open_one_posting_and_none_when_it_says_none(tmp_path):
    with registry.replaced(world(tmp_path, BOARD, NONE_IS_ONE, NO_RULES)):
        assert address.is_single_posting("https://board-x.example.test/post/12")
        assert address.is_single_posting("https://www.board-x.example.test/a/view?id=7")
        assert not address.is_single_posting("https://board-x.example.test/post/abc")
        assert not address.is_single_posting("https://board-x.example.test/view?id=x")
        assert not address.is_single_posting("https://board-e.example.test/post/12")  # a board that says none is one
        assert address.is_board_link("https://board-e.example.test/post/12")


def test_a_board_with_no_rule_leaves_its_links_to_the_generic_rule(tmp_path):
    shape = "/careers/engineer-manager-remote"  # a title of three words under a careers section: the generic rule's
    with registry.replaced(world(tmp_path, NO_RULES, NONE_IS_ONE)):
        assert address.is_single_posting("https://board-n.example.test" + shape) is True
        assert address.is_single_posting("https://board-e.example.test" + shape) is False  # a board that says none
    assert address.is_single_posting("https://www.contoso.example.test" + shape) is True


def host_with(rank: int) -> dict:
    return {"key": "host_h", "kind": "host", "hosts": ["host-h.example.test"],
            "address": {"one_posting": [{"rank": rank, "path": "p/[0-9]+"}]}}


@pytest.mark.parametrize("rank", [0, 1, 2, 3])
def test_a_sites_own_shape_names_one_posting_wherever_among_the_generic_shapes_it_is_asked(tmp_path, rank):
    with registry.replaced(world(tmp_path, host_with(rank))):
        assert address.is_single_posting("https://host-h.example.test/p/12")
        assert not address.is_single_posting("https://host-h.example.test/p/abc")


def test_a_site_with_a_shape_of_its_own_is_asked_no_generic_shape_after_it(tmp_path):
    link = "https://host-h.example.test/job/abcdef12ghij"  # the shape of a hosted careers site's posting
    assert address.is_single_posting(link.replace("host-h", "other")) is True
    with registry.replaced(world(tmp_path, host_with(3))):
        assert address.is_single_posting(link) is False
    with registry.replaced(world(tmp_path, host_with(0))):  # a shape asked first stops every one after it too
        assert address.is_single_posting(link) is False


def test_a_site_with_no_shape_of_its_own_keeps_the_generic_rule(tmp_path):
    other = {"key": "host_o", "kind": "host", "hosts": ["host-o.example.test"]}
    with registry.replaced(world(tmp_path, other)):
        assert address.is_single_posting("https://host-o.example.test/job/abcdef12ghij") is True


APPLY = {"key": "host_a", "kind": "host", "hosts": ["host-a.example.test"], "address": {
    "one_posting": [{"rank": 2, "path": "job/[0-9]+"}],
    "apply_page": [{"path": "apply/(?P<id>[0-9]+)", "capture": {"id": "group:id"}, "template": "https://{host}/job/{id}"}]}}


def test_an_apply_form_is_stored_as_the_posting_page_its_runbook_says_follows_from_it(tmp_path):
    with registry.replaced(world(tmp_path, APPLY)):
        assert address.posting_page("https://x.host-a.example.test/Apply/42") == "https://x.host-a.example.test/job/42"
        assert address.posting_page("https://x.host-a.example.test/apply/abc") == "https://x.host-a.example.test/apply/abc"
        assert address.posting_page("https://other.example.test/apply/42") == "https://other.example.test/apply/42"


SYSTEM = {"key": "sys_s", "kind": "job_system", "hosts": ["sys-s.example.test"], "order": 0,
          "flags": {"needs": ["host", "tenant"], "customer_host": True, "why_not": "its list has no public route"},
          "address": {
              "board": [
                  {"host": "(?:rec|rec2)\\.sys-s\\.example\\.test", "query": {"t": "any"},
                   "capture": {"host": "host", "tenant": "query:t"}},
                  {"host": ".*\\.sys-s\\.example\\.test", "customer_host": True, "skip_locale": True,
                   "path": ("(?P<tenant>[^/]+)(?:/(?:(?!board(?:/|$))[^/]+/)*board/(?P<guid>[^/]+)(?:/.*)?|(?:/.*)?)"),
                   "capture": {"host": "host", "tenant": "group:tenant", "guid": "group:guid"}},
                  {"host": ".*\\.sys-s\\.example\\.test", "customer_host": True, "path": ".+"}],
              "one_posting": [{"query": {"pid": "any"}}, {"path": "(?:(?!job(?:/|$))[^/]+/)*job/<id_like>(?:/.*)?"}],
              "board_url": [{"needs": ["host", "tenant", "guid"], "template": "https://{host}/{tenant}/board/{guid}/"}]}}


def system(tmp_path) -> DataSystem:
    return DataSystem(world(tmp_path, SYSTEM).get("sys_s"))  # pyright: ignore[reportArgumentType]


def board(tmp_path, link: str):
    return system(tmp_path).board(Address.of(link))  # pyright: ignore[reportArgumentType]


def test_a_job_system_without_a_reader_is_made_from_its_runbook(tmp_path):
    made = system(tmp_path)
    assert (made.key, made.name, made.domains, made.needs, made.customer_host, made.why_not) == (
        "sys_s", "Sys_S", ("sys-s.example.test",), ("host", "tenant"), True, "its list has no public route")


def test_a_board_is_the_parts_the_first_rule_that_holds_captures(tmp_path):
    assert board(tmp_path, "https://rec.sys-s.example.test/x?t=acme") == {
        "host": "rec.sys-s.example.test", "tenant": "acme"}
    assert board(tmp_path, "https://acme.sys-s.example.test/en-US/ACME1/a/board/g1/z") == {
        "host": "acme.sys-s.example.test", "tenant": "ACME1", "guid": "g1"}
    assert board(tmp_path, "https://acme.sys-s.example.test/ACME1") == {
        "host": "acme.sys-s.example.test", "tenant": "ACME1"}


def test_a_link_on_the_system_that_names_no_customer_is_an_empty_board_and_the_vendors_own_host_is_none(tmp_path):
    assert board(tmp_path, "https://www.sys-s.example.test/x") is None  # the vendor's own host
    assert board(tmp_path, "https://acme.sys-s.example.test/") is None  # nothing in the path
    assert board(tmp_path, "https://acme.other.example.test/ACME1") is None


def test_a_page_is_one_posting_when_a_rule_says_so_and_a_board_link_is_written_when_it_has_every_part(tmp_path):
    made = system(tmp_path)
    shaped = lambda link: made.shaped(Address.of(link), False)  # noqa: E731  # pyright: ignore[reportArgumentType]
    assert shaped("https://acme.sys-s.example.test/x?pid=3") and shaped("https://acme.sys-s.example.test/a/job/R12")
    assert not shaped("https://acme.sys-s.example.test/a/job/x") and not shaped("https://acme.sys-s.example.test/a")
    assert made.board_url({"host": "h.example.test", "tenant": "t", "guid": "g"}) == "https://h.example.test/t/board/g/"
    assert made.board_url({"host": "h.example.test", "tenant": "t"}) == "https://h.example.test/"  # no guid: the host
    assert made.board_url({"tenant": "t"}) is None


def test_a_job_system_with_no_board_rules_is_not_made(tmp_path):
    from app.jobsearch.adapters import unread

    plain = {"key": "sys_p", "kind": "job_system", "hosts": ["sys-p.example.test"], "order": 0}
    with registry.replaced(world(tmp_path, plain, SYSTEM | {"order": 1})):
        assert list(unread._made()) == ["sys_s"]  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("address_, why", [
    ({"one_posting": [{"path": "("}]}, "not a regular expression"),
    ({"one_posting": [{"pth": "a"}]}, "has no key"),
    ({"one_posting": [{"query": {"id": "words"}}]}, "query is a mapping"),
    ({"one_posting": [{"rank": -1}]}, "rank is a whole number"),
    ({"board": [{"capture": {"x": "group:g"}}]}, "which path does not have"),
    ({"board": [{"capture": {"x": "somewhere"}}]}, "capture is a mapping"),
    ({"apply_page": [{"path": "(?P<i>a)", "capture": {"i": "group:i"}, "template": "{nowhere}"}]},
     "uses only the parts"),
    ({"unknown": []}, "has no key"),
    ({"one_posting": {"path": "a"}}, "is a list of rules")])
def test_a_runbook_with_a_wrong_rule_does_not_load_and_says_which(tmp_path, address_, why):
    found = world(tmp_path, {"key": "bad", "kind": "host", "hosts": ["bad.example.test"], "address": address_})
    assert found.get("bad") is None
    [problem] = found.problems
    assert problem.hosts == ("bad.example.test",) and why in problem.reason
    assert found.refusal("bad.example.test") is not None  # the host is refused, not read at the generic pace


def test_every_rule_is_compiled_once_per_runbook(tmp_path):
    found = world(tmp_path, BOARD).get("board_x")
    first = address_rules.rules(found, "one_posting")
    assert first is not None and address_rules.rules(found, "one_posting") is first
    assert address_rules.rules(found, "board") is None


def system_with(*rules: dict) -> dict:
    return {"key": "system_p", "kind": "job_system", "order": 0, "hosts": ["system-p.example.test"],
            "address": {"board": [{"host": "system-p.example.test"}], "one_posting": list(rules)}}


def posting_of(book, link: str):
    a = Address.of(link)
    assert a is not None
    found = address_rules.rules(book, "one_posting")
    assert found is not None
    return address_rules.posting_of(found, address_rules.Subject(a.host, tuple(a.parts), a.query), a.given)


def test_a_rule_that_captures_the_postings_id_gives_it_with_the_address_to_read_it_from(tmp_path):
    rules = ({"path": r"(?:(?!jobs/)[^/]+/)*jobs/(?P<id>\d+)(?:/.*)?", "capture": {"ext_id": "group:id"}},
             {"path": r"(?:(?!jobs(?:/|$))[^/]+/)*[^/]*", "query": {"token": "digits"},
              "capture": {"ext_id": "query:token"}},
             {"path": "plain/[0-9]+"})  # a rule that names no id is a shape, never an id
    reg = world(tmp_path, system_with(*rules))
    [book] = reg.runbooks
    link = "https://system-p.example.test/acme/jobs/4012/apply?x=1"
    assert posting_of(book, link) == ("4012", link)
    assert posting_of(book, "https://system-p.example.test/embed/job?token=77")[0] == "77"
    assert posting_of(book, "https://system-p.example.test/acme/jobs/abc?token=5") is None  # the path says none
    assert posting_of(book, "https://system-p.example.test/acme/jobs") is None
    assert posting_of(book, "https://system-p.example.test/plain/9") is None
    assert posting_of(book, "https://system-p.example.test/x?token=abc") is None


def test_a_capture_of_an_id_names_a_group_the_path_has():
    with pytest.raises(ValueError, match="group"):
        address_rules.compile_rule({"path": "a/[0-9]+", "capture": {"ext_id": "group:id"}})


def test_the_first_name_of_a_host_is_a_part_of_the_board_and_a_vendors_own_host_is_none(tmp_path):
    reg = world(tmp_path, {"key": "system_h", "kind": "job_system", "order": 0, "hosts": ["system-h.example.test"],
                           "address": {"board": [{"host": r".*\.system-h\.example\.test", "customer_host": True,
                                                  "capture": {"slug": "host_name"}}]}})
    [book] = reg.runbooks
    found = address_rules.rules(book, "board")
    assert found is not None

    def board(link: str):
        parsed = Address.of(link)
        assert parsed is not None
        subject = address_rules.Subject(parsed.host, tuple(parsed.parts), parsed.query)
        hit = address_rules.first(found, subject)
        return address_rules.captured(hit[0], hit[1], subject) if hit else None

    assert board("https://acme.system-h.example.test/jobs") == {"slug": "acme"}
    assert board("https://x.y.system-h.example.test/") == {"slug": "x"}
    assert board("https://help.system-h.example.test/") is None
    assert board("https://other.example.test/") is None


def test_a_part_the_site_writes_in_either_case_is_kept_in_lower_case_only_when_the_rule_says_so():
    rule = address_rules.compile_rule({"path": r"(?P<a>[^/]+)/(?P<b>[^/]+)", "lower": ["a"],
                                       "capture": {"a": "group:a", "b": "group:b"}})
    subject = address_rules.Subject("h.example.test", ("AbC", "DeF"), {})
    assert address_rules.captured(rule, address_rules.match(rule, subject), subject) == {"a": "abc", "b": "DeF"}
    with pytest.raises(ValueError, match="lower"):
        address_rules.compile_rule({"path": "a/[0-9]+", "lower": ["a"]})


def test_an_application_form_is_read_for_its_posting_and_is_not_the_page_of_the_posting():
    rules = tuple(address_rules.compile_rule(r) for r in (
        {"path": r"view/(?P<id>\d+)", "capture": {"ext_id": "group:id"}},
        {"path": r"form/(?P<id>\d+)", "capture": {"ext_id": "group:id"}, "form": True}))
    view, form = (address_rules.Subject("h.example.test", tuple(p.split("/")), {}) for p in ("view/7", "form/7"))
    assert address_rules.shaped(rules, view) and not address_rules.shaped(rules, form)
    assert address_rules.posting_of(rules, form, "given") == ("7", "given")
    with pytest.raises(ValueError, match="form"):
        address_rules.compile_rule({"path": "form/[0-9]+", "form": True})


@pytest.mark.parametrize(("value", "holds"), [
    ("19-001", True), ("a.b_c", True), ("R1", True), ("", False), ("a b", False), ("a/b", False), ("a\n", False),
    ("\u00e9", False), ("a%20b", False)])
def test_a_query_value_may_be_asked_to_be_a_plain_word_all_of_it(value, holds):
    rule = address_rules.compile_rule({"query": {"job": "word"}})
    found = address_rules.match(rule, address_rules.Subject("h.example.test", (), {"job": value}))
    assert found is holds
    with pytest.raises(ValueError, match="query"):
        address_rules.compile_rule({"query": {"job": "words"}})


def test_a_part_taken_from_a_query_is_percent_encoded_in_the_link_a_rule_writes_only_when_the_rule_says_so():
    rule = address_rules.compile_rule({
        "path": r"(?P<section>[^/]+)/apply", "query": {"job": "any"}, "encoded": ["job"],
        "capture": {"section": "group:section", "job": "query:job"},
        "template": "https://{host}/{section}/view?job={job}"})
    subject = address_rules.Subject("h.example.test", ("s1", "apply"), {"job": "a b/c&d"})
    got = address_rules.captured(rule, address_rules.match(rule, subject), subject)
    assert got == {"section": "s1", "job": "a%20b%2Fc%26d"}
    plain = address_rules.compile_rule({"query": {"job": "any"}, "capture": {"job": "query:job"}})
    assert address_rules.captured(plain, True, subject) == {"job": "a b/c&d"}
    with pytest.raises(ValueError, match="encoded"):
        address_rules.compile_rule({"path": "a/[0-9]+", "encoded": ["a"]})
