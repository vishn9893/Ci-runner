"""
Pipeline executor — manages the job queue and runs CI pipelines inside Docker
containers (or bare shell as fallback). Streams logs and reports status to GitHub.
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
USE_DOCKER = os.getenv("USE_DOCKER", "true").lower() == "true"
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "python:3.12-slim")
LOG_DIR = Path(os.getenv("LOG_DIR", "/tmp/ci-runner-logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ─── Job model ────────────────────────────────────────────────────────────────

@dataclass
class Job:
    job_id: str
    repo_url: str
    sha: str
    ref: str
    pusher: str
    event: str
    status: str = "queued"          # queued | running | success | failure | error
    pr_number: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    log_path: Optional[str] = None
    exit_code: Optional[int] = None
    steps: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


# ─── In-memory queue ──────────────────────────────────────────────────────────

class JobQueue:
    """Thread-safe in-memory job store.  Swap out for Redis/Postgres in prod."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def enqueue(self, *, repo_url, sha, ref, pusher, event, pr_number=None) -> str:
        job_id = str(uuid.uuid4())[:8]
        job = Job(
            job_id=job_id,
            repo_url=repo_url,
            sha=sha,
            ref=ref,
            pusher=pusher,
            event=event,
            pr_number=pr_number,
        )
        with self._lock:
            self._jobs[job_id] = job
        logger.info(f"[queue] Job {job_id} enqueued")
        return job_id

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def update(self, job_id: str, **kwargs):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                for k, v in kwargs.items():
                    setattr(job, k, v)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in reversed(list(self._jobs.values()))]


# ─── Pipeline parser ──────────────────────────────────────────────────────────

def load_pipeline(repo_dir: str) -> list[dict]:
    """
    Load .ci.yml from the cloned repo.
    Falls back to a default pipeline if not found.
    """
    import yaml

    ci_file = Path(repo_dir) / ".ci.yml"
    if ci_file.exists():
        with open(ci_file) as f:
            config = yaml.safe_load(f)
        steps = config.get("steps", [])
        logger.info(f"Loaded {len(steps)} steps from .ci.yml")
        return steps

    # Default pipeline
    logger.info("No .ci.yml found — using default pipeline")
    return [
        {"name": "install deps", "run": "pip install -r requirements.txt || true"},
        {"name": "lint",         "run": "flake8 . --count --select=E9,F63,F7,F82 --show-source || true"},
        {"name": "test",         "run": "pytest --tb=short || true"},
    ]


# ─── Step runner ──────────────────────────────────────────────────────────────

def run_step(name: str, command: str, cwd: str, log_fh) -> int:
    """Run a single pipeline step, streaming output to the log file."""
    log_fh.write(f"\n{'─'*60}\n▶  {name}\n   $ {command}\n{'─'*60}\n")
    log_fh.flush()

    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    for line in proc.stdout:
        log_fh.write(line)
        log_fh.flush()

    proc.wait()
    status = "✅ passed" if proc.returncode == 0 else f"❌ failed (exit {proc.returncode})"
    log_fh.write(f"\n{status}\n")
    log_fh.flush()
    return proc.returncode


def run_step_docker(name: str, command: str, workspace: str, log_fh) -> int:
    """Run a step inside a Docker container with the repo mounted."""
    docker_cmd = [
        "docker", "run", "--rm",
        "--network", "none",           # no outbound net for safety
        "--memory", "512m",
        "--cpus", "1",
        "-v", f"{workspace}:/workspace",
        "-w", "/workspace",
        DOCKER_IMAGE,
        "bash", "-c", command,
    ]
    log_fh.write(f"\n{'─'*60}\n▶  {name} [docker]\n   $ {command}\n{'─'*60}\n")
    log_fh.flush()

    proc = subprocess.Popen(
        docker_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in proc.stdout:
        log_fh.write(line)
        log_fh.flush()

    proc.wait()
    status = "✅ passed" if proc.returncode == 0 else f"❌ failed (exit {proc.returncode})"
    log_fh.write(f"\n{status}\n")
    log_fh.flush()
    return proc.returncode


# ─── GitHub status reporter ───────────────────────────────────────────────────

def report_github_status(repo_url: str, sha: str, state: str, description: str, job_id: str):
    """Push a commit status to GitHub via the REST API."""
    if not GITHUB_TOKEN:
        logger.info(f"[github] No token — skipping status update ({state}: {description})")
        return

    try:
        from urllib.request import urlopen, Request as URLRequest
        from urllib.parse import urlparse

        # Extract owner/repo from clone URL
        path = urlparse(repo_url).path.rstrip("/").removesuffix(".git")
        owner_repo = path.lstrip("/")

        url = f"https://api.github.com/repos/{owner_repo}/statuses/{sha}"
        payload = json.dumps({
            "state": state,
            "description": description[:140],
            "context": "ci-runner/python",
            "target_url": f"http://localhost:8000/jobs/{job_id}",
        }).encode()

        req = URLRequest(url, data=payload, method="POST")
        req.add_header("Authorization", f"token {GITHUB_TOKEN}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/vnd.github.v3+json")

        with urlopen(req) as resp:
            logger.info(f"[github] Status posted: {state} ({resp.status})")
    except Exception as e:
        logger.error(f"[github] Failed to post status: {e}")


# ─── Main pipeline runner ─────────────────────────────────────────────────────

def run_pipeline_job(job_id: str, queue: JobQueue):
    """
    Full CI pipeline execution:
      1. Clone repo at the specified SHA
      2. Load .ci.yml (or use defaults)
      3. Execute each step (Docker or shell)
      4. Report status back to GitHub
    """
    job_data = queue.get_job(job_id)
    if not job_data:
        logger.error(f"Job {job_id} not found")
        return

    repo_url = job_data["repo_url"]
    sha      = job_data["sha"]
    ref      = job_data["ref"]

    log_path = LOG_DIR / f"{job_id}.log"
    queue.update(job_id,
                 status="running",
                 started_at=datetime.utcnow().isoformat(),
                 log_path=str(log_path))

    report_github_status(repo_url, sha, "pending", "CI pipeline running…", job_id)

    overall_rc = 0
    step_results = []

    with open(log_path, "w") as log_fh:
        log_fh.write(f"CI Runner — Job {job_id}\n")
        log_fh.write(f"Repo:   {repo_url}\n")
        log_fh.write(f"Ref:    {ref}\n")
        log_fh.write(f"SHA:    {sha}\n")
        log_fh.write(f"Time:   {datetime.utcnow().isoformat()}\n\n")

        try:
            with tempfile.TemporaryDirectory() as tmp:
                # ── Clone ────────────────────────────────────────────────────
                log_fh.write("═"*60 + "\nCLONING REPOSITORY\n" + "═"*60 + "\n")
                log_fh.flush()

                clone_rc = run_step(
                    "clone",
                    f"git clone --depth 50 {repo_url} .",
                    tmp,
                    log_fh,
                )
                if clone_rc != 0:
                    raise RuntimeError("Clone failed")

                # ── Checkout SHA ─────────────────────────────────────────────
                checkout_rc = run_step(
                    "checkout",
                    f"git checkout {sha}",
                    tmp,
                    log_fh,
                )
                if checkout_rc != 0:
                    raise RuntimeError(f"Checkout of {sha} failed")

                # ── Load pipeline ─────────────────────────────────────────────
                steps = load_pipeline(tmp)

                log_fh.write("\n" + "═"*60 + f"\nPIPELINE — {len(steps)} steps\n" + "═"*60 + "\n")
                log_fh.flush()

                # ── Execute steps ─────────────────────────────────────────────
                runner = run_step_docker if USE_DOCKER else run_step

                for step in steps:
                    name    = step.get("name", step.get("run", "step")[:40])
                    command = step.get("run", "echo 'no command'")
                    continue_on_error = step.get("continue-on-error", False)

                    rc = runner(name, command, tmp, log_fh)
                    step_results.append({"name": name, "exit_code": rc})

                    if rc != 0 and not continue_on_error:
                        overall_rc = rc
                        break

        except Exception as exc:
            logger.exception(f"Job {job_id} crashed: {exc}")
            log_fh.write(f"\n💥 Runner error: {exc}\n")
            overall_rc = 1

        # ── Summary ──────────────────────────────────────────────────────────
        log_fh.write("\n" + "═"*60 + "\nSUMMARY\n" + "═"*60 + "\n")
        for s in step_results:
            icon = "✅" if s["exit_code"] == 0 else "❌"
            log_fh.write(f"  {icon}  {s['name']}\n")
        log_fh.write(f"\nOverall: {'PASSED' if overall_rc == 0 else 'FAILED'}\n")

    status    = "success" if overall_rc == 0 else "failure"
    desc      = "All steps passed" if overall_rc == 0 else "One or more steps failed"

    queue.update(job_id,
                 status=status,
                 finished_at=datetime.utcnow().isoformat(),
                 exit_code=overall_rc,
                 steps=step_results)

    report_github_status(repo_url, sha, status, desc, job_id)
    logger.info(f"[runner] Job {job_id} finished: {status}")
