FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-17-jre-headless ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY guandan_ai ./guandan_ai
RUN python -m pip install --no-cache-dir . && python -m guandan_ai fetch-platform
EXPOSE 8502 8503
CMD ["python", "-m", "guandan_ai", "play", "--host", "0.0.0.0", "--port", "8502", "--ws-port", "8503", "--store", "/app/data/server/rooms.sqlite3"]
