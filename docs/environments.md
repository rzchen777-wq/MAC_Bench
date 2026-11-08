# Environments

[Home](../README.md) · [Getting started](getting-started.md) · [Tasks](tasks.md) · [Evaluation](evaluation.md) · [Agent integration](agents.md) · **Environments**

The evaluator and agent system run on your machine; Modal hosts isolated task environments. First use prepares the required image. Later episodes reuse the prepared image and load independent task states.

Install `pip install -e '.[modal,agents]'`, load the dataset, and authenticate with `modal token new`. The [getting-started guide](getting-started.md) covers this sequence.

## Business operations

Business tasks use Twenty, Pretix, HRMS and BigCapital as required by the task. Shared application images provide the services; per-task data supplies the initial records and configuration.

```text
data/environments/images/             Shared application images (OCI)
data/tasks/<task_id>/environment/      Task configuration and initial data
```

On first use, the loader imports the application images and caches the prepared VM, then restores the task data. Shared OCI layers are reused across tasks. `mac-bench data download` installs the selected tasks' image layers and the small shared OCI metadata index; loading a task requires no other task's image layers. Each episode receives an independent environment; resets recreate its containers and restore the initial volumes. Initial records are checked after loading and resetting.

The environment cache follows application configuration, initial data, and image contents. Editing task instructions, tool bindings, or verifiers reuses that cache and installs the current runtime when opening the task. Resets reuse the installed runtime.

The default business VM has **4 CPUs and 8 GiB of memory**. Pretix runs two web workers and two background workers. Independent applications start concurrently, and tool calls reuse a persistent connection. Set VM CPU and memory allocations with `MAC_BENCH_CPUS` and `MAC_BENCH_MEMORY_MIB`. Local preparation records are stored under `.cache/business/<task_id>/`.

## Software engineering

Software tasks have a dependency image and a per-episode source workspace. The loader installs base and locked dependencies in one sandbox and saves a single dependency image. It loads starter code and public fixtures, then runs the task's initialization command. Private tests run in a separate grading sandbox.

Prepare an image ahead of evaluation:

```bash
mac-bench environment prepare --task-id flit_packages
```

Add `--rebuild` to prepare the image again. The cache is replaced after preparation succeeds; running episodes keep their selected image.

Tasks with the same dependency recipe can reuse the cached image while receiving separate workspaces. Local dependency records live in `.cache/environments/`; set `MAC_BENCH_ENVIRONMENT_CACHE` to choose another directory.

## Check loading and reset

Inspect local metadata, then perform a complete load/reset check:

```bash
mac-bench environment describe --task-id op26_service_administration_007
mac-bench environment check \
  --task-id op26_service_administration_007 --out outputs/environment-check
```

`describe` reads metadata without launching a sandbox. `check` opens the environment, checks its initial state, resets it, checks again and closes it. The check uses cloud compute but no model calls.

A successful preschool check reports these fields in `report.json`:

```json
{
  "initial": {"matches": true},
  "reset": {"matches": true}
}
```

This excerpt omits the task identity, digests and runtime details in the full report. A matching digest after reset confirms that the observed state returned to its starting point.

## Lifecycle and resource settings

All workers in an episode share one environment. Software command tools share a 6 GiB resident-memory budget across concurrent commands and their subprocesses. If the total exceeds it, the largest command is stopped while the workspace remains available. Independent episodes receive isolated environments. The evaluator closes task and grader sandboxes when an episode finishes or raises an error; prepared images and local output files remain available for reuse or inspection.

| Setting or location | Purpose |
| --- | --- |
| `MAC_BENCH_CPUS` | Business VM CPU allocation; default 4 |
| `MAC_BENCH_MEMORY_MIB` | Business VM memory; default 8192 MiB |
| `MAC_BENCH_ENVIRONMENT_CACHE` | Software dependency cache directory |
| `.cache/business/<task_id>/` | Business environment preparation records |
| `.cache/environments/` | Software dependency image records |
| Evaluation `--out` directory | Episode results and logs |

Removing a local cache record triggers preparation again. Remote images and task data remain separate. Use [Modal's dashboard](https://modal.com/apps) to inspect active resources in your account.

## Docker client and build contexts

The [Docker guide](../docker/README.md) runs the evaluation client in a container with the same Modal task loaders. It also explains how to export Docker build contexts for software dependencies and task workspaces:

```bash
mac-bench environment docker-context \
  --task-id flit_packages --out outputs/docker/flit_packages
```

The exported `base/` and `task/` directories contain Dockerfiles and build instructions. Business application images are distributed in OCI format.
