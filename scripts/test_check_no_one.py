"""Tests for check_no_one.py: stdlib unittest, run by CI (python -m unittest discover -s scripts)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_no_one as g  # noqa: E402

# Samples are built from pieces so this file would pass its own check even if it were not exempt.
LAN = "192." + "168.7.9"
ROOT_PATH = "/ro" + "ot/data"
HOME_PATH = "C:" + "\\Users\\someone"
NAMED_WIN = "C:" + "\\Users\\jdoe\\AppData"
NAMED_UNIX = "/ho" + "me/jdoe/x"
NEUTRAL_WIN = "C:" + "\\Users\\someone\\x"
NEUTRAL_UNIX = "/Us" + "ers/someone/x"
PLACEHOLDER_UNIX = "/Us" + "ers/<name>/x"
ENV_WIN = "C:" + "\\Users\\%USERNAME%\\x"
TERM = "Quillfeather"


def found(path: str, text: str, terms=None) -> list[str]:
    return g.problems(path, text, terms)


class ShapeRules(unittest.TestCase):
    def test_a_private_memory_note_id_is_refused_anywhere(self):
        note = "mun" + "inn n12345"
        self.assertEqual(len(found("app/x.py", f"# see {note}")), 1)
        self.assertEqual(len(found("docs/a.md", f"(the owner, 2026-11-14; {note.upper()})")), 1)
        self.assertEqual(found("docs/a.md", "the " + "mun" + "inn wiki holds notes"), [])

    def test_the_owner_narrated_as_he_is_refused_in_instructions_and_templates_only(self):
        line = "Keep the words he wrote."
        self.assertEqual(len(found("app/instructions/a.instructions.md", line)), 1)
        self.assertEqual(len(found("app/templates/a.template.md", "Ask him first.")), 1)
        self.assertEqual(found("app/instructions/a.instructions.md", "Ask the owner; they decide. The shelf."), [])
        self.assertEqual(found("app/backend/app/x.py", "# " + line), [])

    def test_a_private_address_is_refused_in_source_and_allowed_in_tests(self):
        self.assertEqual(len(found("app/x.py", f'HOST = "{LAN}"')), 1)
        self.assertEqual(found("app/backend/tests/test_guard.py", 'assert refused("192.168.1.20")'), [])
        self.assertEqual(found("app/frontend/src/a.test.ts", 'refused("192.168.1.20")'), [])

    def test_the_other_private_ranges_and_near_misses(self):
        self.assertEqual(len(found("a.sh", "ssh 10." + "0.0.5")), 1)
        self.assertEqual(len(found("a.sh", "ssh 172." + "16.0.1")), 1)
        self.assertEqual(found("a.sh", "ssh 172." + "32.0.1"), [])
        self.assertEqual(found("a.sh", "version 110." + "0.0.5.1"), [])

    def test_a_home_path_is_refused(self):
        self.assertEqual(len(found("a.sh", f"cd {ROOT_PATH}")), 1)
        self.assertEqual(len(found("a.md", f"key at {HOME_PATH}\\.ssh")), 1)
        self.assertEqual(len(found("a.md", "see /Us" + "ers/someone/x")), 1)
        self.assertEqual(len(found("a.md", "see /ho" + "me/someone/x")), 1)

    def test_the_scout_images_own_home_is_fine(self):
        self.assertEqual(found("app/scout/Dockerfile", "ENV HOME=/ho" + "me/node"), [])

    def test_an_email_must_sit_at_a_reserved_domain(self):
        self.assertEqual(found("a.py", '"pat@example.test" "x@mail.example.co.uk" "a@b.example" "me@x.invalid"'), [])
        self.assertEqual(len(found("a.py", '"someone@' + 'realmail.com"')), 1)

    def test_an_image_name_and_a_package_version_are_not_addresses(self):
        self.assertEqual(found("a.css", "url(logo@2x.png) pkg@1.2.3 typescript@latest"), [])

    def test_a_public_mailbox_provider_is_allowed_only_in_the_mail_reader_fixtures(self):
        line = '"friend@' + 'gmail.com"'
        self.assertEqual(found("app/backend/tests/mailmine/test_mail_reader.py", line), [])
        self.assertEqual(len(found("app/backend/app/x.py", line)), 1)

    def test_a_maintainer_section_is_skipped_except_for_named_homes_and_addresses(self):
        text = f"before\n<!-- maintainer:start -->\nroot {ROOT_PATH} and pat@realmail.example.com\n<!-- maintainer:end -->\nafter {LAN}\n"
        self.assertEqual(found("CLAUDE.md", text), ["CLAUDE.md:5: a private network address"])

    def test_a_maintainer_section_may_not_name_a_user_home_or_a_lan_address(self):
        text = f"<!-- maintainer:start -->\nrun {NAMED_WIN}\nssh {LAN}\nopen {NAMED_UNIX}\n<!-- maintainer:end -->\n"
        self.assertEqual(
            found("docs/a.md", text),
            ["docs/a.md:2: a path under a named user's home folder", "docs/a.md:3: a private network address",
             "docs/a.md:4: a path under a named user's home folder"],
        )

    def test_a_neutral_user_name_or_placeholder_is_fine_in_a_maintainer_section(self):
        line = f"{NEUTRAL_WIN} {NEUTRAL_UNIX} {PLACEHOLDER_UNIX} {ENV_WIN}"
        self.assertEqual(found("docs/a.md", f"<!-- maintainer:start -->\n{line}\n<!-- maintainer:end -->\n"), [])

    def test_a_test_file_may_use_only_the_neutral_private_addresses(self):
        for ok in ("192.168.1.20", "192.168.0.5", "10.0.0.5", "172.16.0.1"):
            self.assertEqual(found("app/backend/tests/test_guard.py", f'refused("{ok}")'), [], ok)
        for bad in ("192.168." + "50.201", "10." + "20.30.40", "172.20." + "1.1"):
            self.assertEqual(
                found("app/backend/tests/test_guard.py", f'refused("{bad}")'),
                ["app/backend/tests/test_guard.py:1: a private network address that is not one of the neutral test addresses"],
                bad,
            )

    def test_the_rules_own_files_and_lock_files_are_not_read(self):
        self.assertEqual(found("scripts/check_no_one.py", f"{LAN}"), [])
        self.assertEqual(found("app/frontend/package-lock.json", f"{LAN}"), [])

    def test_a_hidden_tooling_folder_is_not_read(self):
        self.assertEqual(found(".claude/skills/x/data.json", f"{LAN} {ROOT_PATH}"), [])


class PrivateTerms(unittest.TestCase):
    def terms(self, *lines: str):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "private-terms.txt"
            p.write_text("# mine\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
            return g.read_terms(p)

    def test_a_term_is_found_whole_and_without_regard_to_case_and_never_printed(self):
        terms = self.terms(TERM, "Two Words")
        self.assertEqual(found("docs/a.md", f"the {TERM.upper()} said", terms), ["docs/a.md:1: a term from your private-terms file"])
        self.assertEqual(len(found("docs/a.md", "ends with two  words", terms)), 0)
        self.assertEqual(len(found("docs/a.md", "ends with two words", terms)), 1)
        self.assertEqual(found("docs/a.md", f"{TERM}s and un{TERM}", terms), [])

    def test_a_quoted_or_punctuated_term_matches_as_written(self):
        terms = self.terms('"nick"', "99,000")
        self.assertEqual(len(found("app/x.py", 'who == "nick"', terms)), 1)
        self.assertEqual(found("app/x.py", "a nick in the road", terms), [])
        self.assertEqual(len(found("app/x.py", "floor 99,000.", terms)), 1)

    def test_the_plugin_extension_and_tooling_folders_are_not_searched(self):
        terms = self.terms(TERM)
        for path in ("plugins/resumebot/a.md", "resumebot-browser-plugin/a.js", ".claude/a.md"):
            self.assertEqual(found(path, TERM, terms), [])

    def test_the_census_places_file_is_not_searched_for_terms(self):
        terms = self.terms(TERM)
        self.assertEqual(found("app/backend/app/jobsearch/measures/data/us_places.csv", f"{TERM},ST,1.0,2.0", terms), [])

    def test_the_table_of_windows_time_zones_is_not_searched_for_terms(self):
        terms = self.terms(TERM)
        self.assertEqual(found("app/backend/app/windows_zones.py", f'    "{TERM} Standard Time": "Area/{TERM}",', terms), [])
        self.assertNotEqual(found("app/backend/app/setup_cli.py", f'ZONE = "Area/{TERM}"', terms), [])

    def test_without_terms_only_the_shape_rules_run(self):
        self.assertEqual(found("docs/a.md", TERM, None), [])

    def test_a_term_inside_a_maintainer_section_is_skipped(self):
        terms = self.terms(TERM)
        text = f"<!-- maintainer:start -->\n{TERM}\n<!-- maintainer:end -->\n"
        self.assertEqual(found("CLAUDE.md", text, terms), [])


# The site-name rule is proved with a made-up board; the guard holds no real name.
BOARD_TERMS = [
    {"name": "Example Board", "hosts": ["exampleboard.test"], "verdict": "forbids"},
    {"name": "Other Board", "hosts": ["otherboard.test"], "verdict": "allows"},
]
SHIPPED = g.SHIPPED_RUNBOOKS
OTHER_RUNBOOK = {"key": "otherkey", "name": "Other Board", "hosts": ["otherboard.test"]}
RUNBOOKS = {
    SHIPPED + "otherkey.json": OTHER_RUNBOOK,
    g.RESTRICTED_DIR + "runbooks/example.json": {"key": "exampleboard", "name": "Example Board", "hosts": ["exampleboard.test"]},
}
NAMES = g.build_site_names(BOARD_TERMS, RUNBOOKS)
MIGRATION = "app/backend/migrations/versions/0999_x.py"
MIGRATION_TEXT = (
    '"""Move the stored token."""\n\n# the old board is Example Board\nOLDER = "exampleboard"\n\n\ndef upgrade():\n    pass\n'
)


def lines_of(path, text, allowed=None, exported=False):
    return g.site_hits(path, text, NAMES, allowed, exported)


class SiteNameRule(unittest.TestCase):
    def test_a_name_is_flagged_in_code_comments_and_docs_whatever_its_spelling(self):
        for text in ("# reads Example Board jobs", "x = 'exampleboard.test'", "def read_exampleboard(): ...",
                     "class ExampleBoardUrl: ...", "EXAMPLEBOARD_COUNTS = 1"):
            self.assertEqual(lines_of("app/backend/app/x.py", text)[0], [1], text)
        self.assertEqual(lines_of("docs/a.md", "one\ntwo exampleboard\n")[0], [2])

    def test_a_name_inside_a_longer_word_is_not_a_hit(self):
        self.assertEqual(lines_of("app/x.py", "exampleboards, myexampleboard, example boardgames")[0], [])

    def test_a_name_in_a_path_is_flagged(self):
        self.assertTrue(g.path_hit("app/backend/app/jobsearch/exampleboard/read.py", NAMES))
        self.assertTrue(g.path_hit("app/backend/app/exampleboard_sql.py", NAMES))
        self.assertFalse(g.path_hit("app/backend/app/jobsearch/board/read.py", NAMES))

    def test_the_allowed_places_may_name_a_site(self):
        text = "Example Board and Other Board"
        for path in (g.TERMS_PATH, g.RESTRICTED_DIR + "code/read.py", g.RESTRICTED_DIR + "tests/test_exampleboard.py",
                     "plugins/resumebot/skills/a.md", "resumebot-browser-plugin/a.js"):
            self.assertEqual(lines_of(path, text)[0], [], path)
        self.assertFalse(g.path_hit(g.RESTRICTED_DIR + "code/exampleboard.py", NAMES))

    def test_the_baseline_and_allowance_files_are_not_scanned_for_names(self):
        for path in (g.BASELINE_FILE, g.ALLOW_FILE):
            self.assertEqual(lines_of(path, "app/exampleboard/x.py:3 Example Board")[0], [], path)

    def test_the_places_the_owner_did_not_list_are_not_allowed(self):
        for path in (".claude/skills/a.md", "evals/cases/a.json", "plugins/resumebot-setup/a.md", "CLAUDE.md",
                     "docs/spec/a.md", "app/backend/tests/test_a.py"):
            self.assertEqual(lines_of(path, "Example Board")[0], [1], path)

    def test_a_shipped_runbook_may_name_only_its_own_site(self):
        own = SHIPPED + "otherkey.json"
        self.assertEqual(lines_of(own, "Other Board otherboard.test otherkey")[0], [])
        self.assertEqual(lines_of(own, "Example Board")[0], [1])
        self.assertFalse(g.path_hit(SHIPPED + "otherkey.json", NAMES))
        self.assertTrue(g.path_hit(SHIPPED + "exampleboard.json", NAMES))

    def test_exported_mode_allows_the_terms_file_and_a_shipped_runbook_for_its_own_site_and_nothing_else(self):
        text = "Example Board"
        self.assertEqual(lines_of(g.TERMS_PATH, text, exported=True)[0], [])
        self.assertEqual(lines_of(SHIPPED + "otherkey.json", "Other Board", exported=True)[0], [])
        for path in (g.RESTRICTED_DIR + "code/a.py", "plugins/resumebot/a.md", "app/x.py"):
            self.assertEqual(lines_of(path, text, exported=True)[0], [1], path)
        self.assertTrue(g.path_hit(g.RESTRICTED_DIR + "code/exampleboard.py", NAMES, exported=True))

    def test_exported_mode_does_not_read_installed_packages_a_check_left_in_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path, text in {g.TERMS_PATH: json.dumps(BOARD_TERMS), "app/frontend/node_modules/a/x.js": "Example Board"}.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text, encoding="utf-8")
            self.assertEqual(g.exported_problems(root), [])

    def test_exported_mode_on_a_tree_flags_the_restricted_folder_and_fields_by_file_and_line_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                g.TERMS_PATH: json.dumps(BOARD_TERMS),
                SHIPPED + "otherkey.json": json.dumps(OTHER_RUNBOOK),
                "app/x.py": "ok = 1\n",
            }
            for path, text in files.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text, encoding="utf-8")
            self.assertEqual(g.exported_problems(root), [])
            (root / SHIPPED / "otherkey.json").write_text(json.dumps(dict(OTHER_RUNBOOK, robots={})), encoding="utf-8")
            (root / "app/x.py").write_text("# Example Board\n", encoding="utf-8")
            (root / g.RESTRICTED_DIR).mkdir(parents=True)
            (root / g.RESTRICTED_DIR / "a.json").write_text("{}", encoding="utf-8")
            found = g.exported_problems(root)
            self.assertEqual(sorted(f.split(":")[0] for f in found),
                             sorted([g.RESTRICTED_DIR + "a.json", SHIPPED + "otherkey.json", "app/x.py"]))
            self.assertFalse(any("xample" in f for f in found))

    def test_a_name_in_a_released_migrations_code_is_allowed_by_its_frozen_digest_but_not_in_its_comments(self):
        allowed = {MIGRATION: [g.Allowance("code", g.code_digest(MIGRATION_TEXT))]}
        self.assertEqual(lines_of(MIGRATION, MIGRATION_TEXT, allowed), ([3], []))  # the comment, not the code on line 4
        self.assertEqual(lines_of(MIGRATION, MIGRATION_TEXT)[0], [3, 4])  # a migration not in the list: both
        docstring = MIGRATION_TEXT.replace("Move the stored token.", "Move the Example Board token.")
        self.assertEqual(lines_of(MIGRATION, docstring, {MIGRATION: [g.Allowance("code", g.code_digest(docstring))]})[0], [1, 3])

    def test_a_scrubbed_comment_is_not_a_hit_and_does_not_change_the_digest(self):
        digest = g.code_digest(MIGRATION_TEXT)
        scrubbed = MIGRATION_TEXT.replace("# the old board is Example Board", "# the old board")
        self.assertEqual(g.code_digest(scrubbed), digest)
        self.assertEqual(lines_of(MIGRATION, scrubbed, {MIGRATION: [g.Allowance("code", digest)]}), ([], []))

    def test_a_changed_code_digest_fails(self):
        allowed = {MIGRATION: [g.Allowance("code", g.code_digest(MIGRATION_TEXT))]}
        changed = MIGRATION_TEXT.replace('OLDER = "exampleboard"', 'NEWER = "exampleboard"')
        lines, failures = lines_of(MIGRATION, changed, allowed)
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith(MIGRATION + ":1:"))
        self.assertNotIn("xample", failures[0])
        self.assertEqual(lines, [3, 4])

    def test_a_new_migration_that_names_a_site_fails(self):
        self.assertEqual(lines_of("app/backend/migrations/versions/0998_y.py", MIGRATION_TEXT, {MIGRATION: []})[0], [3, 4])

    def test_a_file_suppression_holds_only_while_its_content_hash_does(self):
        text = "Example Board\n"
        allowed = {"data/a.csv": [g.Allowance("file", g.content_hash(text))]}
        self.assertEqual(lines_of("data/a.csv", text, allowed)[0], [])
        self.assertEqual(lines_of("data/a.csv", text + "more\n", allowed)[0], [1])
        self.assertEqual(lines_of("data/a.csv", text.replace("\n", "\r\n"), allowed)[0], [])  # line endings are not content

    def test_a_path_allowance_lets_the_path_itself_carry_a_name_not_the_content(self):
        path = "app/backend/migrations/versions/0997_exampleboard.py"
        allowed = {path: [g.Allowance("name", "-")]}
        self.assertTrue(g.path_hit(path, NAMES))
        self.assertFalse(g.path_hit(path, NAMES, allowed))
        self.assertEqual(lines_of(path, "# Example Board", allowed)[0], [1])

    def test_only_a_hit_that_is_not_in_the_baseline_fails(self):
        scan = g.SiteScan(lines={"a.py": [3, 4], "b.py": [1]}, paths={"c.py"})
        self.assertEqual(g.new_site_problems(scan, {"a.py": 2, "b.py": 1}, {"c.py"}), [])
        self.assertEqual(g.new_site_problems(scan, {"a.py": 5, "b.py": 9}, {"c.py"}), [])  # fewer is fine
        more = g.new_site_problems(scan, {"a.py": 1, "b.py": 1}, {"c.py"})
        self.assertEqual([m.split(":")[:2] for m in more], [["a.py", "3"], ["a.py", "4"]])
        self.assertEqual(len(g.new_site_problems(scan, {"a.py": 2, "b.py": 1}, set())), 1)  # a new path
        self.assertEqual(len(g.new_site_problems(scan, {}, {"c.py"})), 3)  # two files not listed

    def test_the_baseline_is_written_and_read_back(self):
        scan = g.SiteScan(lines={"a.py": [3, 4]}, paths={"a.py", "c.py"})
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "baseline.txt"
            file.write_text(g.baseline_text(scan), encoding="utf-8")
            self.assertEqual(g.read_baseline(file), ({"a.py": 2}, {"a.py", "c.py"}))

    def test_a_host_stem_is_the_registrable_domains_own_label(self):
        self.assertEqual(g.host_stem("jobs.exampleboard.test"), "exampleboard")
        self.assertEqual(g.host_stem("a.exampleboard.co.uk"), "exampleboard")

    def test_without_names_the_rule_finds_nothing(self):
        self.assertEqual(g.site_hits("a.py", "Example Board", g.build_site_names([], {})), ([], []))


class Main(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).resolve().parents[1] / ".git").exists(), "needs a git checkout")
    def test_a_missing_terms_file_is_said_and_is_not_an_error(self):
        out = self._run(["--terms", "no-such-terms-file.txt"])
        self.assertIn("shape rules only", out)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / ".git").exists(), "needs a git checkout")
    def test_the_repository_itself_is_clean_of_the_shape_rules(self):
        self.assertIn("clean", self._run(["--terms", "no-such-terms-file.txt"]))

    @staticmethod
    def _run(argv):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = g.main(argv)
        assert code == 0, buf.getvalue()
        return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
