"""Fixtures for the site tests."""

import pytest

from app.sites import site_check


@pytest.fixture
def check_says(monkeypatch):
    """The stand-in for `site.check`: each item key is on plan unless the test names a difference for it."""
    differences: dict[str, str] = {}

    async def fake(session, instance, data):
        key = data["item"]["key"]
        if key in differences:
            return {"on_plan": False, "difference": differences[key]}
        return {"on_plan": True, "difference": ""}

    monkeypatch.setattr(site_check, "site_check", fake)
    return differences
