FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Chicago \
    PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir . && \
    apt-get update && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

EXPOSE 8080

CMD ["python", "-m", "argos.main"]
