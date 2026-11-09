# Environments

[Home](../README.md) · [Getting started](getting-started.md) · [Tasks](tasks.md) · [Evaluation](evaluation.md) · [Agent integration](agents.md) · **Environments**

The evaluator and agent system run on your machine. Modal or Daytona hosts the task environment. Each episode loads an independent task state; prepared images are reused between episodes.

## Choose Modal or Daytona

Select the provider for each task family:

| Setting | Values | Default |
| --- | --- | --- |
| `MAC_BENCH_BUSINESS_PROVIDER` | `modal`, `daytona` | `modal` |
| `MAC_BENCH_SOFTWARE_PROVIDER` | `modal`, `daytona` | `modal` |

The business provider also hosts the agent's script workspace. The software provider hosts the task workspace, independent grader and package consumer. A running task retains its selected backend and prepared image through grading and reset.

**Modal**

```bash
pip install -e '.[modal,agents]'
modal token new
```

**Daytona**

```bash
pip install -e '.[daytona,agents]'
export DAYTONA_API_KEY='your-key'
export MAC_BENCH_BUSINESS_PROVIDER=daytona
export MAC_BENCH_SOFTWARE_PROVIDER=daytona
```

These settings can also be saved in the checkout's `.env`; exported environment variables take precedence.

`DAYTONA_TARGET` defaults to `us`; `DAYTONA_API_URL` defaults to `https://app.daytona.io/api`. Both providers load the same task packages and use the same task tools, initial-state checks and verifiers.

## Business operations

Business tasks use Twenty, Pretix, HRMS and BigCapital. OCI application images provide the services; each task supplies its configuration, initial records and public materials.

```text
data/environments/images/             Application images and shared OCI layers
data/tasks/<task_id>/environment/      Task configuration and initial data
```

`mac-bench data download` retrieves the selected tasks' image layers and the shared metadata index. On first use, the loader prepares a cloud image from these resources. Subsequent episodes reuse it, start the applications and check the initial records. Reset recreates containers and restores the task's initial volumes.

Application state determines the preparation cache key. The current tool code and verifier are installed when opening an episode. Independent applications start concurrently; tool calls reuse a persistent connection. Modal caches imported Docker images. Daytona imports OCI layers when a new sandbox opens and releases transfer files as they are consumed. Reset reuses the imported images on both platforms.

## Software engineering

Software environments contain the compilers and locked dependencies required by the task. Preparation installs these once in a clean sandbox and saves a dependency image. Each episode loads starter source and public fixtures into a fresh workspace, then runs the task's initialization command.

Submitted code is graded in a separate sandbox using the same dependency image. Package tasks also use an independent consumer to check generated artifacts. Task commands and grading run offline, with localhost available for application tests. Reset creates a fresh workspace from the initial source.

Prepare dependencies ahead of an evaluation:

```bash
mac-bench environment prepare --task-id flit_packages
```

Add `--rebuild` to prepare a new image from the original base. The cache record is replaced after preparation succeeds. Tasks with the same dependency recipe reuse the image.

## Check loading and reset

```bash
mac-bench environment describe --task-id op26_service_administration_007
mac-bench environment check \
  --task-id op26_service_administration_007 --out outputs/environment-check
```

`describe` reads local metadata. `check` launches a sandbox, checks the initial state, resets it, checks again and closes it. It uses cloud compute without model calls. For a business task, `report.json` includes:

```json
{
  "initial": {"matches": true},
  "reset": {"matches": true}
}
```

The full report also records the task identity, state digests and runtime details.

## Resources and cache

Business and software task hosts default to **4 CPUs and 8 GiB RAM**. Daytona allocates **10 GiB disk** by default and requires whole CPU cores and GiB memory allocations. A separate business script workspace uses 0.5 CPU / 512 MiB on Modal or 1 CPU / 1 GiB on Daytona.

| Setting | Purpose |
| --- | --- |
| `MAC_BENCH_CPUS` | Business host CPU allocation |
| `MAC_BENCH_MEMORY_MIB` | Business host memory in MiB |
| `MAC_BENCH_DAYTONA_DISK_GIB` | Daytona task disk allocation |
| `MAC_BENCH_ENVIRONMENT_CACHE` | Prepared-image record directory; default `.cache/environments/` |
| Evaluation `--out` | Episode results and logs |

Preparation records are stored as `<business|software>/<modal|daytona>/<cache-key>/image.json`. They identify the cloud image, input recipe and provider scope. Cache entries are locked during preparation and checked against the cloud before reuse. Each run records its selected image in its environment diagnostics.

All agents in an episode share the task state. Software commands share a 6 GiB resident-memory budget, including subprocesses; the largest command is stopped if their combined usage exceeds it. Evaluation budgets, individual command timeouts and provider lifecycle settings serve different purposes. The evaluator closes task, grader and workspace sandboxes at episode end. Modal task hosts have a two-hour maximum lifetime; Daytona reclaims sandboxes after two hours of inactivity. Prepared images remain available in your cloud account.

Deleting a preparation record causes a fresh preparation on the next use. Inspect cloud resources in the [Modal dashboard](https://modal.com/apps) or [Daytona dashboard](https://app.daytona.io).

## Docker client and build contexts

The [Docker guide](../docker/README.md) runs the evaluation client in a container with either cloud backend. To export software dependency and workspace build materials:

```bash
mac-bench environment docker-context \
  --task-id flit_packages --out outputs/docker/flit_packages
```

The exported `base/` and `task/` directories contain Dockerfiles and build instructions. Business application images use OCI format.
