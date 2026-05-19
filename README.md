# CI Runner

A webhook-based GitHub Actions–style CI runner written in Python.

## Architecture

```
GitHub Push / PR
       │
  Webhook POST
       ▼
  FastAPI Server          (api/webhook.py)
       │
  Job enqueued
       ▼
  Background worker       (worker/executor.py)
       │
  Clone repo @ SHA
       │
  Load .ci.yml            (pipeline/parser.py)
       │
  Execute steps
  (Docker or shell)
       │
  Report status ──────► GitHub Commit Status API
       │
  Logs saved to disk
```

## Quick start

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Run the server (shell-mode, no Docker required)
USE_DOCKER=false uvicorn api.webhook:app --reload

# 3. Test a manual trigger
curl -X POST http://localhost:8000/trigger \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/your/repo.git"}'

# 4. Check job status
curl http://localhost:8000/jobs
```

## Docker (production)

```bash
docker-compose up --build
```

## Environment variables

| Variable         | Default     | Description                                  |
|------------------|-------------|----------------------------------------------|
| `WEBHOOK_SECRET` | `changeme`  | GitHub webhook secret (set to anything to skip validation) |
| `GITHUB_TOKEN`   | _(empty)_   | PAT for posting commit statuses              |
| `USE_DOCKER`     | `true`      | Run steps inside Docker containers           |
| `DOCKER_IMAGE`   | `python:3.12-slim` | Image used for Docker execution        |
| `LOG_DIR`        | `/tmp/ci-runner-logs` | Where logs are written              |

## GitHub webhook setup

1. Go to your repo → **Settings → Webhooks → Add webhook**
2. Payload URL: `https://your-server/webhook`
3. Content type: `application/json`
4. Secret: same value as `WEBHOOK_SECRET`
5. Events: **Pushes** and **Pull requests**

## Pipeline format (.ci.yml)

Place a `.ci.yml` in the root of your repo:

```yaml
name: My Pipeline

steps:
  - name: install
    run: pip install -r requirements.txt

  - name: test
    run: pytest -v
    continue-on-error: false
    timeout: 300
```

If no `.ci.yml` is found, the runner uses a default install → lint → test pipeline.

## API endpoints

| Method | Path            | Description                    |
|--------|-----------------|--------------------------------|
| GET    | `/health`       | Liveness check                 |
| GET    | `/jobs`         | List all jobs                  |
| GET    | `/jobs/{id}`    | Get a specific job             |
| POST   | `/webhook`      | GitHub webhook receiver        |
| POST   | `/trigger`      | Manual trigger (for testing)   |

## Running tests

```bash
pip install pytest httpx
pytest tests/ -v
```
