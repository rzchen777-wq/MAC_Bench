# Tests

Install the development dependencies and run the local suite:

```bash
sudo apt-get install ripgrep
pip install -e '.[dev,agents]'
pytest
```

| Location | What it checks |
| --- | --- |
| `test_*.py` | Dataset indexes, downloads, packaging and documented examples |
| `business/` | Business tools, permissions and partial-operation feedback |
| `environments/` | Environment loading, reset, resource use and RPC transport |
| `evaluation/` | Episode lifecycle, budgets, scoring and baseline adapters |
| `runtime/` | Model interfaces, context management, terminal and workspace tools |
| `tasks/` | Public materials and outcome verifiers in the installed task packages |

The tests use scripted model responses and local environments. They do not run paid model calls or start Modal sandboxes. Personal `.env` files are disabled during tests.

## Task packages

Install the task data as described in [Getting started](../docs/getting-started.md), then run:

```bash
pytest tests/tasks
```

These checks also run with ordinary `pytest` when the data is installed. A code-only checkout reports them as skipped. `MAC_BENCH_TASK_ROOT` can point to another installed `tasks/` directory. For browser checks, install Playwright and Chromium:

```bash
pip install playwright
python -m playwright install chromium
```

## Native baseline integration

Fetch the pinned AOrchestra, Meta-Team and Magentic-One checkouts and install their adapters:

```bash
python scripts/fetch_baselines.py --name AOrchestra
python scripts/fetch_baselines.py --name Meta-Team
python scripts/fetch_baselines.py --name Magentic-One
pip install -e '.[dev,agents,aorchestra,meta_team,magentic_one]'
pytest --run-baselines
```

This adds tests of the original orchestration loops, worker messaging and interruption handling. Model responses remain scripted. Use `pytest --run-baselines -m baselines` to run only this group.
