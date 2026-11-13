"""Property tests: a prop day is the half-open interval between two reset instants."""

import datetime as dt
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from propbot.core.propday import prop_day, prop_day_start

# Zones whose DST changes happen in the small hours, away from midnight.
ZONES = st.sampled_from(
    [ZoneInfo(key) for key in ("Europe/Prague", "America/New_York", "UTC", "Europe/Istanbul")]
)
RESETS = st.builds(dt.time, st.integers(0, 23), st.sampled_from([0, 30]))
MOMENTS = st.datetimes(
    min_value=dt.datetime(2020, 1, 1),
    max_value=dt.datetime(2030, 12, 31),
    timezones=st.just(dt.UTC),
)


@given(moment=MOMENTS, zone=ZONES, reset=RESETS)
def test_moment_lies_inside_its_prop_day(
    moment: dt.datetime, zone: ZoneInfo, reset: dt.time
) -> None:
    day = prop_day(moment, zone, reset)

    assert prop_day_start(day, zone, reset) <= moment
    assert moment < prop_day_start(day + dt.timedelta(days=1), zone, reset)


@given(moment=MOMENTS, minutes=st.integers(0, 3 * 24 * 60), zone=ZONES, reset=RESETS)
def test_prop_day_never_goes_backwards(
    moment: dt.datetime, minutes: int, zone: ZoneInfo, reset: dt.time
) -> None:
    later = moment + dt.timedelta(minutes=minutes)

    assert prop_day(moment, zone, reset) <= prop_day(later, zone, reset)


@given(moment=MOMENTS, zone=ZONES)
def test_midnight_reset_is_the_local_calendar_date(moment: dt.datetime, zone: ZoneInfo) -> None:
    assert prop_day(moment, zone, dt.time(0, 0)) == moment.astimezone(zone).date()
