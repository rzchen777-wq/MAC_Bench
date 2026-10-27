# Evaluation

Select a task, system and budget explicitly:

```bash
mac-bench evaluate --task-id black_formatter \
  --method react_single --repetitions 1 --max-tokens 1000000 \
  --out outputs/black-react
```

This is a single-task run with a 1M-token budget. Set the budget and repetitions consistently when comparing systems. `--max-seconds`, `--max-model-calls` and `--max-tool-calls` provide additional limits shared by the team.

Use `--dry-run` to view the selected configuration without allocating an environment. Use `--inspect` to view the public task interface. Custom method settings can be supplied with `--method-config`.

## Baselines

ReAct is included in the core installation. Other adapters use their upstream implementations:

```bash
python scripts/fetch_baselines.py --name Meta-Team
pip install -e '.[meta_team]'
```

The other names are `AOrchestra` and `Magentic-One`, with extras `aorchestra` and `magentic_one`. Checkouts are stored in `.cache/baselines/`; their source commits are specified in `configs/baselines/upstream.json`. Each baseline uses the same task interface and shared episode budget.

## Results

The output directory contains:

- `experiment.json`: task and method configuration for the run.
- `summary.json`: episode outcomes, aggregate PS/RR and completion status.
- `episode_*.json`: per-episode results and artifact locations.
- `attempts/`: model/tool traces, runtime logs, submissions and verifier results.

An environment or verifier error is recorded separately from a valid task failure. Read the episode's logs to identify where execution stopped.

Each invocation runs fresh episodes by default. `--reuse-completed` reuses completed episodes with matching recorded configurations. Reuse skips a completed run; it does not replay business writes into a live environment.
