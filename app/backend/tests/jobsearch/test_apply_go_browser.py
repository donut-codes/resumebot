"""How Go opens a batch: one new Chrome window, else the default browser (and says so)."""

import subprocess

from app.jobsearch.browser import go as apply_go

LINKS = ["https://a.example.test/1", "https://b.example.test/2", "https://c.example.test/3"]
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def test_the_command_is_one_new_window_with_the_batch_in_order_and_the_everyday_profile():
    cmd = apply_go.chrome_command(CHROME, LINKS)
    assert cmd == [CHROME, "--new-window", *LINKS]
    assert not any(a.startswith("--user-data-dir") for a in cmd)


async def test_with_chrome_it_launches_once_and_adds_no_note(monkeypatch):
    ran = []
    monkeypatch.setattr(apply_go, "find_chrome", lambda: CHROME)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd: ran.append(cmd))
    monkeypatch.setattr(apply_go.webbrowser, "open_new", lambda u: ran.append(("default", u)))
    assert await apply_go.open_links(LINKS) is None
    assert ran == [[CHROME, "--new-window", *LINKS]]


async def test_without_chrome_the_default_browser_gets_a_new_window_then_tabs_and_a_note(monkeypatch):
    calls = []
    monkeypatch.setattr(apply_go, "find_chrome", lambda: None)
    monkeypatch.setattr(apply_go.webbrowser, "open_new", lambda u: calls.append(("window", u)))
    monkeypatch.setattr(apply_go.webbrowser, "open_new_tab", lambda u: calls.append(("tab", u)))
    note = await apply_go.open_links(LINKS, pause=0)
    assert calls == [("window", LINKS[0]), ("tab", LINKS[1]), ("tab", LINKS[2])]
    assert note == apply_go.NO_CHROME


async def test_an_empty_batch_opens_nothing(monkeypatch):
    def refuse():
        raise AssertionError("nothing should be looked for")

    monkeypatch.setattr(apply_go, "find_chrome", refuse)
    assert await apply_go.open_links([]) is None
