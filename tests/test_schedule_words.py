"""A schedule written in words becomes the schedule text of the routines; the weekday-of-the-month forms of the cron work."""
import datetime as dt

import pytest

from tadamoo.planner.dag import PlanError
from tadamoo.planner.schedule_words import to_schedule
from tadamoo.scheduler import Schedule


@pytest.mark.parametrize("words, expected", [
    ("every 6 hours", "every 6h"), ("ogni sei ore", "every 6h"), ("ogni due giorni", "every 2d"), ("every 2 days", "every 2d"), ("every 15 minutes", "every 15m"),
    ("ogni mezz'ora", "every 30m"), ("ogni ora", "every 1h"), ("ogni settimana", "every 7d"), ("every 2 weeks", "every 14d"),
    ("ogni primo lunedì", "cron 0 9 * * 1#1"), ("ogni primo lunedì del mese alle 9", "cron 0 9 * * 1#1"), ("every first Monday at 9", "cron 0 9 * * 1#1"),
    ("ogni ultima domenica del mese alle 15", "cron 0 15 * * 0L"), ("ogni ultima domenica del mese alle tre di pomeriggio", "cron 0 15 * * 0L"),
    ("every last Sunday of the month at 3pm", "cron 0 15 * * 0L"), ("ogni terzo giovedì del mese alle 17:30", "cron 30 17 * * 4#3"),
    ("il 15 di ogni mese alle 10", "cron 0 10 15 * *"), ("on the 15th of every month at 10:00", "cron 0 10 15 * *"), ("ogni primo del mese", "cron 0 9 1 * *"),
    ("ogni lunedì alle 8:30", "mon 08:30"), ("ogni lunedì e venerdì alle 18", "mon,fri 18:00"), ("ogni sabato a mezzogiorno", "sat 12:00"),
    ("every day at 8:30", "daily 08:30"), ("ogni giorno alle 7", "daily 07:00"), ("ogni giorno", "daily 09:00"), ("tutti i giorni alle 6", "daily 06:00"),
    ("ogni giorno feriale alle 8", "weekdays 08:00"), ("every weekend at 10", "weekends 10:00"),
    ("daily 08:30", "daily 08:30"), ("cron 0 15 * * 0L", "cron 0 15 * * 0L"), ("0 15 * * 1#1", "cron 0 15 * * 1#1"),
])
def test_words_become_a_schedule(words, expected):
    text, _ = to_schedule(words)
    assert text == expected
    Schedule.parse(text)                                                    # and the routines understand it


@pytest.mark.parametrize("words", ["", "bla bla", "ogni 2 giorni alle 8", "every 3 hours at 9", "ogni sesto lunedì", "on the 40th of every month"])
def test_what_is_not_understood_is_refused_with_an_example(words):
    with pytest.raises(PlanError, match="for example|time of day|from 1 to 31|empty|up to 200"):
        to_schedule(words)


def day(text, since):
    return dt.datetime.fromtimestamp(Schedule.parse(text).next_after(since.timestamp()))


def test_the_first_monday_and_the_last_sunday_of_the_month_come_on_those_days():
    since = dt.datetime(2026, 11, 13, 12, 0)
    first, again = day("cron 0 9 * * 1#1", since), None
    assert (first.year, first.month, first.day, first.hour, first.strftime("%A")) == (2026, 12, 7, 9, "Monday")
    again = day("cron 0 9 * * 1#1", first)
    assert (again.year, again.month, again.day) == (2027, 1, 4)
    last = day("cron 0 15 * * 0L", since)
    assert (last.year, last.month, last.day, last.hour, last.strftime("%A")) == (2026, 11, 29, 15, "Sunday")
    assert day("cron 0 15 * * 0L", last).day == 27                          # December's last Sunday
    assert day("cron 0 15 * * 0L", dt.datetime(2027, 1, 31, 15, 0)).day == 28      # February 2027
    assert day("cron 0 9 * * 1-5,0L", since).strftime("%A") == "Monday"            # the plain list still works beside them


def test_a_cron_with_a_weekday_of_the_month_that_is_not_one_is_refused():
    from tadamoo.scheduler import RoutineError
    for bad in ("cron 0 9 * * 1#6", "cron 0 9 * * 9L", "cron 0 9 * * x#1"):
        with pytest.raises(RoutineError):
            Schedule.parse(bad)
