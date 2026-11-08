"""Unit tests for the scheduler.

The scheduler is the one component that runs unattended for weeks, so its
pure logic — the cron matcher and the job state machine — is covered
exhaustively here. The parts that need a live Neo4j are covered by the
integration suite instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fin_kg.scheduler.runner import (
    JobDefinition,
    Scheduler,
    _cron_matches,
    _field_matches,
    build_scheduler,
)


class TestFieldMatcher:
    @pytest.mark.parametrize("value", [0, 1, 30, 59])
    def test_wildcard_matches_everything(self, value: int) -> None:
        assert _field_matches("*", value)

    def test_exact_number(self) -> None:
        assert _field_matches("15", 15)
        assert not _field_matches("15", 16)

    def test_list(self) -> None:
        assert _field_matches("1,15,30", 30)
        assert not _field_matches("1,15,30", 31)

    def test_range(self) -> None:
        assert _field_matches("9-17", 12)
        assert not _field_matches("9-17", 18)

    def test_step(self) -> None:
        assert _field_matches("*/15", 30)
        assert not _field_matches("*/15", 31)

    def test_invalid_step_is_ignored(self) -> None:
        assert not _field_matches("*/0", 30)
        assert not _field_matches("*/abc", 30)

    def test_invalid_range_is_ignored(self) -> None:
        assert not _field_matches("a-b", 30)

    def test_invalid_number_is_ignored(self) -> None:
        assert not _field_matches("abc", 30)

    def test_whitespace_is_tolerated(self) -> None:
        assert _field_matches(" 1 , 15 ", 15)

    def test_a_list_matches_when_any_part_matches(self) -> None:
        assert _field_matches("1,*/15", 30)


class TestCronMatcher:
    def _at(
        self,
        minute: int,
        hour: int = 3,
        day: int = 8,
        month: int = 11,
    ):
        """Build a moment on a fixed date."""
        return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)

    def _on_weekday(self, weekday: int, *, minute: int = 0, hour: int = 3):
        """Build a moment that falls on the given weekday (0=Monday)."""
        moment = self._at(minute, hour=hour)
        return moment + timedelta(days=(weekday - moment.weekday()) % 7)

    def test_a_wrong_field_count_never_matches(self) -> None:
        assert not _cron_matches("0 2 * *", self._at(0))
        assert not _cron_matches("0 2 * * * *", self._at(0))

    def test_exact_time(self) -> None:
        assert _cron_matches("30 2 8 11 *", self._at(30, hour=2))

    def test_every_minute(self) -> None:
        assert _cron_matches("* * * * *", self._at(37))

    def test_the_configured_expressions(self) -> None:
        """The four expressions the shipped configuration uses."""
        assert _cron_matches("0 2 * * *", self._at(0, hour=2))
        assert _cron_matches("0 */6 * * *", self._at(0, hour=6))
        assert _cron_matches("0 */6 * * *", self._at(0, hour=12))
        assert _cron_matches("*/15 * * * *", self._at(45))
        assert _cron_matches("0 3 * * *", self._at(0, hour=3))

    def test_a_non_matching_time(self) -> None:
        assert not _cron_matches("0 2 * * *", self._at(0, hour=9))

    def test_weekday_is_one_based_monday_first(self) -> None:
        """The matcher uses 1=Monday .. 7=Sunday, unlike cron.

        The difference matters: a cron expression with ``0`` in the weekday
        field would mean Sunday here, so the convention is pinned by a test
        rather than left to a reader's guess.
        """
        # 2026-11-08 is a Sunday -> weekday index 6 in datetime (Mon=0).
        sunday = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
        assert sunday.weekday() == 6
        assert _cron_matches("0 3 * * 7", sunday)  # 7 = Sunday
        assert not _cron_matches("0 3 * * 1", sunday)  # 1 = Monday

    def test_weekday_one_is_monday(self) -> None:
        monday = self._on_weekday(0)
        assert monday.weekday() == 0
        assert _cron_matches("0 3 * * 1", monday)


class TestJobDefinition:
    def test_as_dict_reports_the_state(self) -> None:
        job = JobDefinition(name="ingest", cron="0 2 * * *", handler=lambda: None)
        payload = job.as_dict()
        assert payload["name"] == "ingest"
        assert payload["cron"] == "0 2 * * *"
        assert payload["lastStatus"] == "never"
        assert payload["runCount"] == 0


class TestScheduler:
    def _scheduler_with(self, handler) -> Scheduler:
        scheduler = Scheduler()
        scheduler.register(JobDefinition(name="test", cron="* * * * *", handler=handler))
        return scheduler

    def test_run_job_returns_the_result(self) -> None:
        scheduler = self._scheduler_with(lambda: {"rows": 7})
        outcome = scheduler.run_job(scheduler.jobs[0])
        assert outcome["status"] == "succeeded"
        assert outcome["result"] == {"rows": 7}

    def test_a_failing_job_is_recorded_not_raised(self) -> None:
        def boom() -> None:
            raise RuntimeError("job exploded")

        scheduler = self._scheduler_with(boom)
        outcome = scheduler.run_job(scheduler.jobs[0])
        assert outcome["status"] == "failed"
        assert "job exploded" in outcome["error"]
        assert scheduler.jobs[0].last_status == "failed"

    def test_the_run_count_increments(self) -> None:
        scheduler = self._scheduler_with(lambda: None)
        scheduler.run_job(scheduler.jobs[0])
        scheduler.run_job(scheduler.jobs[0])
        assert scheduler.jobs[0].run_count == 2

    def test_disabled_jobs_are_skipped(self) -> None:
        scheduler = Scheduler()
        scheduler.register(
            JobDefinition(name="off", cron="* * * * *", handler=lambda: None, enabled=False)
        )
        assert scheduler.run_all_once() == []

    def test_run_all_once_runs_every_enabled_job(self) -> None:
        scheduler = Scheduler()
        scheduler.register(JobDefinition(name="a", cron="* * * * *", handler=lambda: {"n": 1}))
        scheduler.register(JobDefinition(name="b", cron="* * * * *", handler=lambda: {"n": 2}))
        outcomes = scheduler.run_all_once()
        assert len(outcomes) == 2
        assert {item["result"]["n"] for item in outcomes} == {1, 2}

    def test_status_reports_every_job(self) -> None:
        scheduler = self._scheduler_with(lambda: None)
        status = scheduler.status()
        assert len(status) == 1
        assert status[0]["name"] == "test"

    def test_start_is_idempotent(self) -> None:
        scheduler = self._scheduler_with(lambda: None)
        scheduler.start()
        try:
            scheduler.start()  # must not raise
        finally:
            scheduler.stop()

    def test_stop_is_safe_without_start(self) -> None:
        Scheduler().stop()


class TestBuildScheduler:
    def _settings(self):
        from fin_kg.config.settings import Settings

        return Settings()

    def test_the_four_configured_jobs_are_registered(self) -> None:
        scheduler = build_scheduler(
            self._settings(),
            handlers={
                "ingest": lambda: None,
                "rescore": lambda: None,
                "propagate": lambda: None,
                "research": lambda: None,
            },
        )
        assert {job.name for job in scheduler.jobs} == {
            "ingest",
            "rescore",
            "propagate",
            "research",
        }

    def test_a_missing_handler_means_no_job(self) -> None:
        scheduler = build_scheduler(self._settings(), handlers={"ingest": lambda: None})
        assert [job.name for job in scheduler.jobs] == ["ingest"]

    def test_the_cron_expressions_come_from_the_settings(self) -> None:
        settings = self._settings()
        scheduler = build_scheduler(settings, handlers={"ingest": lambda: None})
        assert scheduler.jobs[0].cron == settings.scheduler.job_ingest_cron
