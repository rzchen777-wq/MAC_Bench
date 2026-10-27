# Docker

## Evaluation client with Modal environments

This image runs the evaluation client. Task environments run on Modal, using the same loaders as a host Python installation.

From the repository root:

```bash
docker build -f docker/Dockerfile -t mac-bench .
docker run --rm mac-bench tasks list
```

For evaluations, put the model configuration and `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` in your local `.env`, then mount task data read-only and outputs separately:

```bash
docker compose -f docker/compose.yaml run --rm benchmark \
  evaluate --task-id op26_service_administration_007 \
  --method react_single --repetitions 1 --max-tokens 1000000 \
  --out outputs/react
```

The client image contains code and the task index. It does not bake credentials, task snapshots or experiment outputs into image layers. The Compose service mounts `.cache/` for dependency receipts and downloaded baseline sources.

## Software environment build contexts

Export the dependency setup and public workspace for an installed software task:

```bash
mac-bench environment docker-context \
  --task-id black_formatter --out outputs/docker/black_formatter
```

The output contains two build contexts:

- `base/`: base image and fixed dependency setup.
- `task/`: starter source, public materials and task initialization.

Build commands are included in the exported README. The task workspace excludes private tests and reference code. These are Docker build materials; complete scoring uses the Modal evaluation harness above.
