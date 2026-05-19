FROM python:3.12-slim

WORKDIR /app

# System deps (git + docker CLI for Docker-in-Docker mode)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/log/ci-runner

EXPOSE 8000

CMD ["uvicorn", "api.webhook:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
