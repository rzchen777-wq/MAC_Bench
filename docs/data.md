# Task data

MAC-Bench contains 48 test tasks and 6 development tasks. Browse the task index before loading an environment:

```bash
mac-bench tasks list --split dev
mac-bench tasks show op26_service_administration_007
```

## Install data

Extract the dataset archive into the repository root:

```bash
tar -xzf mac-bench-data.tar.gz
```

The archive contains `data/tasks/` and shared environment assets under `data/environments/`. Each task is an ordinary directory. You can read its instructions, inspect its resources and evaluator, or edit a local copy.

For a release served over HTTP, download only the tasks you need using its `downloads.json` URL:

```bash
mac-bench data download --url "$MAC_BENCH_DATA_URL" --split dev
```

Add `--task-id TASK_ID` to select one task. Shared environment files are reused across downloads.

You can also install a task archive or copy a task from another checkout:

```bash
mac-bench data install --task-id black_formatter --archive black_formatter.tar.gz
mac-bench data import --task-id black_formatter --source /path/to/checkout
```

Business tasks also use the shared application images in `data/environments/images/`.

## Inspect and modify

The task index is `data/tasks.json`. Each task directory contains:

| Path | Contents |
| --- | --- |
| `public/` | Instructions and materials given to the agent |
| `private/` | Evaluator configuration, initial-state checks and reference materials |
| `environment/` | Business application configuration and initial data |
| `manifest.json` | Task metadata and release checksums |

`private/` identifies evaluator-owned resources. These are readable by benchmark users; they are not included in the agent's task interface. Shared business tools live in `src/mac_bench/environments/assets/business_tools/`.

Normal loading accepts local edits. To check whether installed files match the original release, run:

```bash
mac-bench data verify --task-id black_formatter
```

## Export

```bash
mac-bench data export --out outputs/tasks
```

This writes one archive per task and an `archives.json` with sizes and checksums. Shared application images are distributed separately from per-task archives.
