"""
Pipeline parser — loads and validates .ci.yml pipeline definitions.

Supported .ci.yml format
------------------------

name: My Pipeline           # optional
on: [push, pull_request]    # optional; not enforced here

env:                        # optional global env vars
  NODE_ENV: test

steps:
  - name: install
    run: pip install -r requirements.txt

  - name: test
    run: pytest -v
    continue-on-error: false
    timeout: 300             # seconds (not yet enforced in executor)
    env:
      DEBUG: "1"

  - name: build image
    run: docker build -t myapp .
    if: ${{ success() }}    # simple condition (not yet evaluated)
"""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ─── Validation helpers ────────────────────────────────────────────────────────

REQUIRED_STEP_KEYS = {"run"}
KNOWN_STEP_KEYS = {"name", "run", "continue-on-error", "env", "timeout", "if"}


def _validate_step(step: Any, index: int) -> dict:
    if not isinstance(step, dict):
        raise ValueError(f"Step {index} must be a mapping, got {type(step).__name__}")

    missing = REQUIRED_STEP_KEYS - step.keys()
    if missing:
        raise ValueError(f"Step {index} is missing required keys: {missing}")

    unknown = set(step.keys()) - KNOWN_STEP_KEYS
    if unknown:
        logger.warning(f"Step {index} has unknown keys (will be ignored): {unknown}")

    return {
        "name":             step.get("name", step["run"][:50]),
        "run":              step["run"],
        "continue-on-error": bool(step.get("continue-on-error", False)),
        "timeout":          int(step.get("timeout", 600)),
        "env":              step.get("env", {}),
    }


# ─── Public API ───────────────────────────────────────────────────────────────

def load_pipeline(repo_dir: str) -> list[dict]:
    """
    Load and validate the .ci.yml pipeline from a cloned repository.
    Returns a list of normalised step dicts.
    Raises ValueError for invalid configs, falls back to defaults if missing.
    """
    if not HAS_YAML:
        logger.warning("PyYAML not installed — using default pipeline")
        return _default_pipeline()

    ci_file = Path(repo_dir) / ".ci.yml"
    if not ci_file.exists():
        logger.info("No .ci.yml found — using default pipeline")
        return _default_pipeline()

    try:
        with open(ci_file) as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in .ci.yml: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(".ci.yml must be a YAML mapping at the top level")

    raw_steps = raw.get("steps")
    if not raw_steps:
        raise ValueError(".ci.yml must have a non-empty 'steps' list")

    steps = [_validate_step(s, i) for i, s in enumerate(raw_steps)]
    logger.info(f"Loaded {len(steps)} steps from .ci.yml")
    return steps


def _default_pipeline() -> list[dict]:
    return [
        {
            "name": "install dependencies",
            "run": "pip install -r requirements.txt 2>/dev/null || echo 'no requirements.txt'",
            "continue-on-error": True,
            "timeout": 300,
            "env": {},
        },
        {
            "name": "lint (fast checks)",
            "run": "flake8 . --count --select=E9,F63,F7,F82 --show-source 2>/dev/null || true",
            "continue-on-error": True,
            "timeout": 60,
            "env": {},
        },
        {
            "name": "run tests",
            "run": "pytest --tb=short -q 2>/dev/null || echo 'pytest not found / no tests'",
            "continue-on-error": True,
            "timeout": 600,
            "env": {},
        },
    ]


# ─── CLI helper ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json

    path = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        steps = load_pipeline(path)
        print(json.dumps(steps, indent=2))
    except ValueError as e:
        print(f"Pipeline error: {e}", file=sys.stderr)
        sys.exit(1)
