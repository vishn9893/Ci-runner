"""
GitHub Webhook Receiver — FastAPI server that validates incoming webhook events
and enqueues CI jobs for processing.
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from worker.executor import JobQueue, run_pipeline_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CI Runner", version="1.0.0")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "changeme")
queue = JobQueue()


def verify_signature(body: bytes, signature: str | None) -> bool:
    """Validate GitHub's HMAC-SHA256 webhook signature."""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/jobs")
async def list_jobs():
    """List all jobs and their current status."""
    return {"jobs": queue.list_jobs()}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if WEBHOOK_SECRET != "changeme" and not verify_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = request.headers.get("X-GitHub-Event", "unknown")
    payload = json.loads(body)

    logger.info(f"Received event: {event}")

    if event == "push":
        repo_url = payload["repository"]["clone_url"]
        sha = payload["after"]
        ref = payload["ref"]
        pusher = payload.get("pusher", {}).get("name", "unknown")

        if sha == "0000000000000000000000000000000000000000":
            return {"ok": True, "message": "Branch deleted, skipping"}

        job_id = queue.enqueue(
            repo_url=repo_url,
            sha=sha,
            ref=ref,
            pusher=pusher,
            event=event,
        )

        background_tasks.add_task(run_pipeline_job, job_id, queue)
        logger.info(f"Enqueued job {job_id} for {sha[:8]} on {ref}")
        return {"ok": True, "job_id": job_id}

    elif event == "pull_request":
        action = payload.get("action")
        if action not in ("opened", "synchronize", "reopened"):
            return {"ok": True, "message": f"Ignoring PR action: {action}"}

        repo_url = payload["repository"]["clone_url"]
        sha = payload["pull_request"]["head"]["sha"]
        ref = payload["pull_request"]["head"]["ref"]
        pr_number = payload["pull_request"]["number"]
        pusher = payload["pull_request"]["user"]["login"]

        job_id = queue.enqueue(
            repo_url=repo_url,
            sha=sha,
            ref=ref,
            pusher=pusher,
            event=event,
            pr_number=pr_number,
        )

        background_tasks.add_task(run_pipeline_job, job_id, queue)
        logger.info(f"Enqueued job {job_id} for PR #{pr_number} sha {sha[:8]}")
        return {"ok": True, "job_id": job_id}

    elif event == "ping":
        return {"ok": True, "message": "pong"}

    return {"ok": True, "message": f"Event '{event}' received but not handled"}


# Manual trigger endpoint for testing without GitHub
@app.post("/trigger")
async def manual_trigger(request: Request, background_tasks: BackgroundTasks):
    """Manually trigger a pipeline run (useful for local testing)."""
    data = await request.json()
    repo_url = data.get("repo_url")
    sha = data.get("sha", "HEAD")
    ref = data.get("ref", "refs/heads/main")

    if not repo_url:
        raise HTTPException(status_code=400, detail="repo_url is required")

    job_id = queue.enqueue(
        repo_url=repo_url,
        sha=sha,
        ref=ref,
        pusher="manual",
        event="manual",
    )

    background_tasks.add_task(run_pipeline_job, job_id, queue)
    return {"ok": True, "job_id": job_id}
