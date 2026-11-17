"""The peak-hours zone default is the provider's published zone, so the window is the same instant for every user."""

from datetime import UTC, datetime

from app.jobsearch.profile.schema import Settings
from app.jobsearch.work.hours import in_peak


def test_default_peak_window_is_the_same_instant_whatever_zone_the_user_lives_in():
    s = Settings()
    assert s.peak_zone == "America/Los_Angeles"
    # Wednesday 2026-03-04 05:00 Pacific (standard time) is 13:00 UTC; the window runs to 19:00 UTC.
    assert not in_peak(s, datetime(2026, 3, 4, 12, 59, tzinfo=UTC))
    assert in_peak(s, datetime(2026, 3, 4, 13, 0, tzinfo=UTC))
    assert in_peak(s, datetime(2026, 3, 4, 18, 59, tzinfo=UTC))
    assert not in_peak(s, datetime(2026, 3, 4, 19, 0, tzinfo=UTC))
    # Daylight saving moves it in UTC, not in Pacific time: 05:00 Pacific is 12:00 UTC in July.
    assert in_peak(s, datetime(2026, 7, 8, 12, 0, tzinfo=UTC))
    assert not in_peak(s, datetime(2026, 7, 8, 11, 59, tzinfo=UTC))
