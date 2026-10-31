FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates ripgrep && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/mac-bench
COPY pyproject.toml LICENSE NOTICE README.md ./
COPY src ./src
COPY configs ./configs
COPY data/public_tasks.json ./data/public_tasks.json
COPY data/tasks.json ./data/tasks.json
COPY data/distribution.json ./data/distribution.json
RUN pip install --no-cache-dir '.[modal,agents]'
ENV MAC_BENCH_ROOT=/opt/mac-bench PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["mac-bench"]
CMD ["tasks", "list"]
