"""Tests for lib/localdata.py's data-folder default: stdlib unittest, run by CI (python -m unittest discover -s scripts)."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import localdata as base  # noqa: E402

STACK = Path(__file__).resolve().parent.parent / "app" / "scripts" / "local" / "stack.py"


def load_stack():
    spec = importlib.util.spec_from_file_location("stack_for_localdata_test", STACK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DefaultDataFolder(unittest.TestCase):
    def test_without_the_variable_it_is_the_users_own_folder(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESUMEBOT_DATA_DIR", None)
            found = base.default_data_dir()
        self.assertNotIn("someone-else", str(found).lower())
        self.assertEqual(found.name, "resumebot")

    def test_it_is_the_folder_stack_py_uses(self):
        # stack.py (and, through its own test, `resumebot init`) holds the rule; scripts/ cannot import it, so the
        # two are held equal here.
        stack = load_stack()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESUMEBOT_DATA_DIR", None)
            self.assertEqual(base.default_data_dir(), stack.default_data_folder())

    def test_the_variable_still_wins(self):
        with mock.patch.dict(os.environ, {"RESUMEBOT_DATA_DIR": "somewhere"}):
            self.assertEqual(base.default_data_dir(), Path("somewhere"))


if __name__ == "__main__":
    unittest.main()
