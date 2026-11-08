# Evaluation

[Home](../README.md) · [Getting started](getting-started.md) · [Tasks](tasks.md) · **Evaluation** · [Agent integration](agents.md) · [Environments](environments.md)

An evaluation episode starts from a task's initial state, runs the selected system under a shared budget, and scores its final state. Each repetition gets a fresh environment.

## Run the included baselines

Complete [installation and model configuration](getting-started.md) first. ReAct is included with the `agents` extra. AOrchestra, Meta-Team and Magentic-One use the pinned checkouts in [upstream.json](../configs/baselines/upstream.json). Superagent uses the DeepSeek Harness packages specified in its pnpm lockfile.

| System | `--method` | Upstream checkout name | Additional extra |
| --- | --- | --- | --- |
| ReAct | `react_single` | Included | `agents` |
| AOrchestra | `aorchestra` | `AOrchestra` | `aorchestra` |
| Magentic-One | `magentic_one` | `Magentic-One` | `magentic_one` |
| Meta-Team | `meta_team` | `Meta-Team` | `meta_team` |
| Superagent | `superagent` | npm packages | `superagent` |

For example, install and run Meta-Team:

```bash
python scripts/fetch_baselines.py --name Meta-Team
pip install -e '.[modal,agents,meta_team]'
mac-bench evaluate \
  --task-id op26_service_administration_007 \
  --method meta_team --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 80 \
  --max-tool-calls 160 --max-seconds 2400 \
  --out outputs/preschool-meta-team
```

Downloaded sources live in `.cache/baselines/`. Method settings are in [configs/baselines](../configs/baselines); pass a modified file with `--method-config path/to/config.yaml`.

## Run Superagent

Superagent routes the task to persistent workers. Workers share the task environment and can delegate to subagents; each agent keeps its own conversation history.

Install [Node.js 24 or newer](https://nodejs.org/) and pnpm 11.7.0, then run from the checkout root:

```bash
pip install -e '.[modal,agents,superagent]'
npm install --global pnpm@11.7.0
pnpm --dir src/mac_bench/baselines/superagent install --frozen-lockfile
```

Configure `MAC_BENCH_LLM_BASE_URL`, `MAC_BENCH_LLM_API_KEY` and `MAC_BENCH_LLM_MODEL` in `.env`. For DeepSeek, use `https://api.deepseek.com` and `deepseek-v4-flash`. Run one development task:

```bash
mac-bench evaluate \
  --task-id op26_service_administration_007 \
  --method superagent --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 3000 \
  --max-tool-calls 9000 --max-seconds 7200 \
  --out outputs/preschool-superagent
```

For GPT-6-Luna through OpenRouter, set `OPENROUTER_API_KEY` and add
`--method-config configs/baselines/superagent_luna.yaml`. This connection uses LiteLLM.
Both configurations disable thinking. Router, workers, subagents and context summaries share the model and episode budget.
To use an HTTP proxy, add `model_transport: {proxy: "http://host:port"}` to your configuration.

The optional Docker target includes Node and DSH:

```bash
docker build --target superagent -f docker/Dockerfile -t mac-bench:superagent .
```

The [Docker guide](../docker/README.md#run-superagent) gives the run command with dataset, cache and output mounts.

## Paper evaluation

The main comparison uses all 48 test tasks, with eight tasks per category and one run per system and backbone. Thinking is disabled. Each run has a two-hour execution limit and a task-specific budget of 5M, 10M or 15M input-plus-output tokens, shared across the whole system. The corresponding model-call ceilings are 1,000, 2,000 and 3,000; tool-call ceilings are 3,000, 6,000 and 9,000. The [task budget file](../configs/evaluation/task_budgets.json) lists the exact limits.

The commands above are single-task examples. For the paper configuration on the first finance test task:

```bash
mac-bench evaluate \
  --task-id op26_business_finance_001 \
  --method superagent --repetitions 1 \
  --max-tokens 15000000 --max-model-calls 3000 \
  --max-tool-calls 9000 --max-seconds 7200 \
  --out outputs/finance-superagent
```

## Choose the run budget

The limits apply to the **whole team**, including its workers and coordination calls. Execution stops admitting new calls when a limit is reached; the final task state is still evaluated.

| Option | Meaning | CLI default |
| --- | --- | ---: |
| `--max-tokens` | Total input and output tokens | 400,000 |
| `--max-model-calls` | Model request attempts | 80 |
| `--max-tool-calls` | Task tool calls | 160 |
| `--max-seconds` | Agent execution time in seconds | 2,400 |
| `--repetitions` | Independent episodes per task | 2 |

Specify these settings explicitly for comparisons. Increasing the token budget alone leaves the other limits unchanged. Token counts are supplied after a model response, so the last response can take the total beyond the token threshold.

Append `--dry-run` to any evaluation command to review its selected task, method and budget. Use the same model, limits and repetition count when comparing systems, and report them with PS and RR.

## Read results

```bash
python examples/read_results.py outputs/preschool-meta-team/summary.json
```

Here is the score portion of a recorded Meta-Team run on `op26_service_administration_005` (a different task from the tutorial):

```text
Task: op26_service_administration_005
System: meta_team | Status: completed
Scored episodes: 1/1
PS: 33.33% | RR: 0.00%
Episode 1: PS 33.33% | RR 0.00% | stop budget:tokens
  Tokens: 9,774,113 input + 283,677 output | Tool calls: 415
```

The run completed evaluation and satisfied some goals, but did not resolve the task. `status: completed` describes execution of the evaluation; `evaluation_status: passed` or `failed` describes the task outcome. PS and RR in JSON use a 0–1 scale.

`summary.json` includes:

| Field | Meaning |
| --- | --- |
| `task_id`, `baseline`, `model` | Evaluated task and system |
| `metrics.ps`, `metrics.rr` | Mean scores over eligible episodes |
| `metrics.scored_episodes` | Number of episodes included in those means |
| `metrics.excluded` | Counts of episodes excluded from the means |
| `episodes` | Individual outcomes, usage and artifact paths |

Episodes with execution or evaluator errors are recorded separately and excluded from aggregate scores. Inspect those errors before reporting a benchmark average. Repetitions are repeated runs of the same task, not additional dataset tasks.

## Inspect an episode

The result reader prints an episode directory under `attempts/`. Its main files are:

```text
attempts/<run-id>/
├── identity.json                # Run configuration
├── runtime/                     # Environment logs and task-specific artifacts
└── episode/
    ├── result.json              # Score, status, usage and stopping reason
    ├── evaluation.json          # Verifier output
    ├── initial.json             # Initial observation
    ├── final.json               # Final observation
    ├── model_calls/             # Recorded model requests and responses
    ├── events.jsonl             # Execution events and tool activity
    ├── trajectory.json         # Adapter-returned trajectory, when available
    └── native/                 # Adapter-specific artifacts, when produced
```

For an unexpected result, start with the stopping reason and `evaluation.json`, then use the model calls and tool events to inspect what the system did. `owner_errors.jsonl` in the episode directory contains runtime tracebacks when an error occurs.

## Repeat or resume a run

Each invocation executes fresh episodes by default. Use a new `--out` directory for a new configuration. To reuse matching completed episodes, add `--reuse-completed`; the output records which episodes were reused and their artifact locations.

For a test-set evaluation, enumerate task IDs with `mac-bench tasks list --split test` and run the same command and settings for each task. Keep task coverage and valid episode counts alongside the aggregate scores.
