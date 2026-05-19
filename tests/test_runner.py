"""
Tests for the CI runner components.
Run with:  pytest tests/ -v
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── Pipeline parser tests ─────────────────────────────────────────────────────

def test_default_pipeline_when_no_ci_yml():
    from pipeline.parser import load_pipeline
    with tempfile.TemporaryDirectory() as tmp:
        steps = load_pipeline(tmp)
    assert len(steps) >= 1
    for step in steps:
        assert "run" in step
        assert "name" in step


def test_load_valid_ci_yml():
    from pipeline.parser import load_pipeline
    config = """
steps:
  - name: install
    run: pip install -r requirements.txt
  - name: test
    run: pytest
    continue-on-error: true
"""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".ci.yml").write_text(config)
        steps = load_pipeline(tmp)

    assert len(steps) == 2
    assert steps[0]["name"] == "install"
    assert steps[1]["continue-on-error"] is True


def test_invalid_ci_yml_missing_run():
    from pipeline.parser import load_pipeline
    config = """
steps:
  - name: no run key here
"""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".ci.yml").write_text(config)
        with pytest.raises(ValueError, match="missing required keys"):
            load_pipeline(tmp)


def test_invalid_ci_yml_empty_steps():
    from pipeline.parser import load_pipeline
    config = "steps: []\n"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".ci.yml").write_text(config)
        with pytest.raises(ValueError, match="non-empty"):
            load_pipeline(tmp)


# ─── JobQueue tests ────────────────────────────────────────────────────────────

def test_job_enqueue_and_retrieve():
    from worker.executor import JobQueue
    q = JobQueue()
    job_id = q.enqueue(
        repo_url="https://github.com/example/repo.git",
        sha="abc123",
        ref="refs/heads/main",
        pusher="alice",
        event="push",
    )
    assert job_id
    job = q.get_job(job_id)
    assert job["status"] == "queued"
    assert job["sha"] == "abc123"


def test_job_update():
    from worker.executor import JobQueue
    q = JobQueue()
    job_id = q.enqueue(
        repo_url="https://github.com/example/repo.git",
        sha="def456",
        ref="refs/heads/main",
        pusher="bob",
        event="push",
    )
    q.update(job_id, status="running")
    assert q.get_job(job_id)["status"] == "running"


def test_missing_job_returns_none():
    from worker.executor import JobQueue
    q = JobQueue()
    assert q.get_job("nonexistent") is None


# ─── Webhook API tests ─────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    # Patch the queue used inside webhook module
    with patch("api.webhook.run_pipeline_job"):
        from api.webhook import app
        return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ping_webhook(client):
    r = client.post(
        "/webhook",
        json={"zen": "Keep it logically awesome.", "hook_id": 1},
        headers={"X-GitHub-Event": "ping"},
    )
    assert r.status_code == 200
    assert r.json()["message"] == "pong"


def test_push_webhook_enqueues_job(client):
    payload = {
        "after": "aabbccdd1122",
        "ref": "refs/heads/main",
        "pusher": {"name": "alice"},
        "repository": {"clone_url": "https://github.com/example/repo.git"},
    }
    r = client.post(
        "/webhook",
        json=payload,
        headers={"X-GitHub-Event": "push"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "job_id" in data


def test_push_delete_branch_skipped(client):
    payload = {
        "after": "0000000000000000000000000000000000000000",
        "ref": "refs/heads/feature",
        "pusher": {"name": "alice"},
        "repository": {"clone_url": "https://github.com/example/repo.git"},
    }
    r = client.post(
        "/webhook",
        json=payload,
        headers={"X-GitHub-Event": "push"},
    )
    assert r.status_code == 200
    assert "skipping" in r.json()["message"]


def test_manual_trigger(client):
    r = client.post("/trigger", json={"repo_url": "https://github.com/example/repo.git"})
    assert r.status_code == 200
    assert "job_id" in r.json()
