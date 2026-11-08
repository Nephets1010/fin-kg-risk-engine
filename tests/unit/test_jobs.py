"""Unit tests for the job store.

The job store is what makes a long-running ingestion observable. Its
contract is small but strict: a job must be findable, its state must
survive a round trip, and a failing job must record the failure rather
than lose it.
"""

from __future__ import annotations

import time

import pytest

from fin_kg.api.jobs import Job, JobStore, RedisJobStore
from fin_kg.core.errors import NotFoundError


class TestJobStore:
    def test_create_returns_a_pending_job(self) -> None:
        store = JobStore()
        job = store.create("ingest")
        assert job.status == "pending"
        assert job.job_type == "ingest"
        assert job.id

    def test_get_returns_the_created_job(self) -> None:
        store = JobStore()
        job = store.create("ingest")
        assert store.get(job.id).id == job.id

    def test_get_raises_for_an_unknown_job(self) -> None:
        with pytest.raises(NotFoundError):
            JobStore().get("nope")

    def test_update_changes_the_mutable_fields(self) -> None:
        store = JobStore()
        job = store.create("ingest")
        store.update(job.id, status="running", progress=42, total=100)
        updated = store.get(job.id)
        assert updated.status == "running"
        assert updated.progress == 42
        assert updated.total == 100

    def test_update_raises_for_an_unknown_job(self) -> None:
        with pytest.raises(NotFoundError):
            JobStore().update("nope", status="running")

    def test_list_returns_the_newest_first(self) -> None:
        store = JobStore()
        first = store.create("a")
        time.sleep(0.01)
        second = store.create("b")
        listed = store.list()
        assert [job.id for job in listed[:2]] == [second.id, first.id]

    def test_list_respects_the_limit(self) -> None:
        store = JobStore()
        for _ in range(5):
            store.create("ingest")
        assert len(store.list(limit=3)) == 3

    def test_run_async_records_the_result(self) -> None:
        store = JobStore()
        job = store.create("ingest")
        store.run_async(job.id, lambda _job: {"rows": 5})
        for _ in range(100):
            if store.get(job.id).status in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        finished = store.get(job.id)
        assert finished.status == "succeeded"
        assert finished.result == {"rows": 5}

    def test_run_async_records_a_failure(self) -> None:
        store = JobStore()
        job = store.create("ingest")

        def boom(_job):
            raise RuntimeError("the ingestion exploded")

        store.run_async(job.id, boom)
        for _ in range(100):
            if store.get(job.id).status in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        finished = store.get(job.id)
        assert finished.status == "failed"
        assert "the ingestion exploded" in (finished.error or "")


class TestJobSerialisation:
    def test_as_dict_matches_the_api_shape(self) -> None:
        job = Job(id="j1", job_type="ingest")
        payload = job.as_dict()
        assert payload["jobId"] == "j1"
        assert payload["jobType"] == "ingest"
        assert payload["status"] == "pending"
        assert set(payload) == {
            "jobId",
            "jobType",
            "status",
            "createdAt",
            "updatedAt",
            "progress",
            "total",
            "result",
            "error",
        }


class FakeRedis:
    """A minimal Redis stand-in — only ``get`` and ``set`` are needed."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class TestRedisJobStore:
    def test_a_job_is_readable_from_another_store(self) -> None:
        """The whole point of the Redis store: any worker can serve it."""
        redis = FakeRedis()
        writer = RedisJobStore(redis)
        job = writer.create("ingest")
        writer.update(job.id, status="running", progress=7)

        reader = RedisJobStore(redis)
        loaded = reader.get(job.id)
        assert loaded.id == job.id
        assert loaded.status == "running"
        assert loaded.progress == 7

    def test_the_key_is_namespaced(self) -> None:
        redis = FakeRedis()
        store = RedisJobStore(redis, key_prefix="custom")
        job = store.create("ingest")
        assert f"custom:{job.id}" in redis.data

    def test_the_serialised_state_round_trips(self) -> None:
        redis = FakeRedis()
        store = RedisJobStore(redis)
        job = store.create("ingest")
        store.update(job.id, status="succeeded", result={"rows": 3})
        loaded = RedisJobStore(redis).get(job.id)
        assert loaded.status == "succeeded"
        assert loaded.result == {"rows": 3}

    def test_an_unknown_key_falls_back_to_the_local_store(self) -> None:
        store = RedisJobStore(FakeRedis())
        with pytest.raises(NotFoundError):
            store.get("nope")
