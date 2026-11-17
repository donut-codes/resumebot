"""The code checks on the Coach's report (docs/PRD.md 15.2.7, 15.2.8, 15.2.10): a claim about the owner cites the master
or the story library, no banned phrase is spoken, a time-sensitive fact carries a dated source, no one job carries
more than two answers, and a question to ask has its good and bad answer. Each refusal is one plain sentence.
Made-up data."""

import copy

from app.jobsearch.interviews import coach_checks
from app.jobsearch.profile.documents import Banned, Writing
from tests.jobsearch.coach_builders import SOURCE, context, good_report


def problems(report=None, **changes) -> list[str]:
    return coach_checks.check_report(report if report is not None else good_report(), context(**changes))


def broken(change) -> list[str]:
    report = good_report()
    change(report)
    return problems(report)


def test_a_report_that_cites_the_master_and_dates_its_sources_passes():
    assert problems() == []


def test_a_missing_section_is_named():
    report = good_report()
    del report["reference"]
    assert any("reference" in p for p in problems(report))


def test_a_claim_about_the_owner_with_no_master_line_is_refused():
    found = broken(lambda r: r["staples"]["opener"][0].update({"from": []}))
    assert any("opener" in p and "master" in p for p in found)


def test_a_citation_that_is_not_in_the_master_or_the_library_is_refused():
    found = broken(lambda r: r["questions"][0]["answer"].update({"from": ["b-nothing"]}))
    assert any("b-nothing" in p for p in found)


def test_why_leaving_may_ask_the_owner_instead_of_inventing_a_reason():
    assert problems() == []
    found = broken(lambda r: r["staples"].update({"why_leaving": {"text": "I wanted a change."}}))
    assert any("why_leaving" in p for p in found)


def test_a_banned_phrase_in_a_spoken_line_is_refused_but_a_count_of_years_is_not():
    writing = Writing(banned=[Banned(id="x", phrase="passionate", why="puffery")])
    report = good_report()
    report["staples"]["opener"][1]["text"] = "I am passionate about programs, with ten years behind me."
    found = problems(report, writing=writing)
    assert any("passionate" in p for p in found)
    assert not any("years" in p for p in found)


def test_a_certification_the_master_does_not_hold_is_refused():
    found = broken(lambda r: r["staples"]["opener"][0].update({"text": "I am a certified PMP who builds programs."}))
    assert any("PMP" in p for p in found)


def test_a_time_sensitive_fact_with_no_dated_source_is_refused():
    found = broken(lambda r: r["reference"].append({"text": "The company raised a round in 2026."}))
    assert any("source" in p and "raised" in p for p in found)


def test_a_source_needs_an_https_address_a_title_and_a_date_no_older_than_yesterday():
    def with_source(**source):
        item = {"text": "The chair changed.", "source": {**SOURCE, **source}}
        return broken(lambda r: r["reference"].__setitem__(0, item))

    assert any("https" in p for p in with_source(url="http://news.example.test/x"))
    assert any("title" in p for p in with_source(title=""))
    assert any("date" in p for p in with_source(checked_on="2026-10-20"))
    assert any("date" in p for p in with_source(checked_on="2026-11-02"))
    assert with_source(checked_on="2026-10-28") == []


def test_the_market_range_needs_a_dated_source():
    found = broken(lambda r: r["staples"]["market"].pop("source"))
    assert any("market" in p and "source" in p for p in found)


def test_with_three_jobs_no_job_carries_more_than_two_answers():
    def crowd(r):
        r["questions"] = [
            {"question": f"Question {n}", "method": "m",
             "answer": {"kind": "story", "title": "t", "text": "x", "result": "y", "from": [line]}}
            for n, line in enumerate(["b-risk", "b-kb", "b-train"])]

    found = broken(crowd)
    assert any("Northwind" in p and "three" in p for p in found)


def test_a_story_that_cites_lines_from_two_jobs_is_refused():
    found = broken(lambda r: r["questions"][0]["answer"].update({"from": ["b-open", "b-risk"]}))
    assert any("two jobs" in p for p in found)


def test_a_question_with_no_story_and_no_gap_is_refused():
    found = broken(lambda r: r["questions"][3].pop("gap"))
    assert any("Tell me about a time you led" in p for p in found)


def test_a_question_to_ask_without_its_good_and_bad_answer_is_refused():
    found = broken(lambda r: r["ask"][0].pop("bad_answer"))
    assert any("bad_answer" in p for p in found)


def test_first_choice_questions_stop_at_the_rounds_budget_unless_the_owner_has_rules_for_interviews():
    def many(r):
        r["ask"] = [{**copy.deepcopy(good_report()["ask"][0]), "round": "panel", "question": f"Question {n}"}
                    for n in range(3)]

    assert any("panel" in p for p in broken(many))
    report = good_report()
    many(report)
    assert problems(report, has_interview_rules=True) == []


def test_every_named_interviewer_has_a_card_with_a_dont():
    assert any("Sam Ortiz" in p for p in broken(lambda r: r["stage_read"]["interviewers"].clear()))
    assert any("don't" in p.lower() for p in broken(lambda r: r["stage_read"]["interviewers"][0].pop("dont")))


def test_an_open_gap_with_no_action_is_refused():
    found = broken(lambda r: r["open_gaps"][0].pop("action"))
    assert any("open gap" in p.lower() for p in found)


def test_the_review_a_second_session_sends_needs_its_findings_whole():
    assert coach_checks.check_review({"summary": "Holds up.", "findings": []}) == []
    assert coach_checks.check_review({"findings": [{"where": "opener"}]}) != []
    assert coach_checks.check_review("nothing") != []
