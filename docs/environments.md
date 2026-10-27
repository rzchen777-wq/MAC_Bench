# Environments

The evaluator and agent system run on your machine; Modal hosts isolated task environments. Install `pip install -e '.[modal]'`, extract the task data, and authenticate with `modal token new`.

## Business tasks

Business tasks use real application services: Twenty, Pretix, HRMS and BigCapital. The dataset separates shared application images from each task's initial records and configuration:

```text
data/environments/images/              # OCI image store; shared layers appear once
data/tasks/<task_id>/environment/       # Service configuration and initial state
```

On first use, the loader creates a VM in your Modal account, loads the task's application images and restores its initial data. It caches this prepared environment in your account for subsequent episodes. Tool definitions and the outcome verifier are installed from the local checkout.

Each episode receives its own application state. A reset recreates the task's containers and restores its initial volumes. The loader checks the restored records against `private/initial.json` before exposing task tools.

The default business VM has 4 CPUs and 16 GiB of memory. Set `MAC_BENCH_CPUS` and `MAC_BENCH_MEMORY_MIB` to change these allocations. Runtime caches live under `.cache/business/<task_id>/`.

## Software tasks

Software environments separate installed dependencies from the task workspace. The loader builds a dependency image in your account, loads starter source and public fixtures, then runs the task's initialization command. Private tests run in a separate grading sandbox.

Prepare a dependency image before evaluation:

```bash
mac-bench environment prepare --task-id black_formatter
```

Dependency images are cached by their base image and setup recipe under `.cache/environments/`. Tasks with the same recipe reuse that image and receive separate workspaces. Set `MAC_BENCH_ENVIRONMENT_CACHE` to choose another cache directory.

## Check loading

```bash
mac-bench environment describe --task-id op26_service_administration_007
mac-bench environment check \
  --task-id op26_service_administration_007 --out outputs/environment-check
```

`describe` reads local metadata. `check` starts the environment, checks its initial state, resets it, checks again and closes it. It uses cloud compute but no solver model. The output directory contains the report and runtime logs.

## Reset and cleanup

All agents in an episode share one environment. Separate episodes use isolated environments. Evaluation closes task and grader sandboxes on completion or error; cached images and local run results remain available.

Delete a task's local cache directory to rebuild its environment. This does not delete task data or a remote Modal image. [Modal's dashboard](https://modal.com/apps) lists active sandboxes and stored images in your account.

## Docker

The [Docker guide](../docker/README.md) provides a containerized evaluation client and software dependency/workspace build contexts. The client uses the same Modal loaders. Business application images use the standard OCI format and can be inspected with tools such as Skopeo.
