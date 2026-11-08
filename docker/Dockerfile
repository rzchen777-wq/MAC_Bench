FROM node:24-bookworm-slim AS node-runtime

FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates ripgrep && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/mac-bench
COPY pyproject.toml LICENSE NOTICE README.md ./
COPY src ./src
COPY configs ./configs
COPY data/public_tasks.json ./data/public_tasks.json
COPY data/tasks.json ./data/tasks.json
COPY data/distribution.json ./data/distribution.json
RUN pip install --no-cache-dir -e '.[modal,agents]'
ENV MAC_BENCH_ROOT=/opt/mac-bench PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["mac-bench"]
CMD ["tasks", "list"]

FROM base AS superagent
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js install --global pnpm@11.7.0 \
    && pip install --no-cache-dir -e '.[superagent]' \
    && cd src/mac_bench/baselines/superagent \
    && pnpm install --frozen-lockfile --prod \
    && python -c "from mac_bench.baselines.superagent.runner import runtime_dependencies; runtime_dependencies()"

FROM base AS default
