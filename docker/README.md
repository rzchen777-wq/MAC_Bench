# Docker

[Home](../README.md) · [Environment guide](../docs/environments.md)

## Run the evaluation client

The Docker image packages the MAC-Bench evaluator and ReAct dependencies. It connects to the same Modal or Daytona environments as a local Python installation.

From the repository root, build the client and browse the development tasks:

```bash
docker build -f docker/Dockerfile -t mac-bench .
docker run --rm mac-bench tasks list --split dev
```

Load the [dataset](../docs/getting-started.md#2-load-the-task-data), then configure your model and sandbox credentials in `.env`:

```dotenv
MAC_BENCH_LLM_BASE_URL=https://your-provider.example/v1
MAC_BENCH_LLM_API_KEY=your-api-key
MAC_BENCH_LLM_MODEL=your-model-name
MODAL_TOKEN_ID=your-modal-token-id
MODAL_TOKEN_SECRET=your-modal-token-secret
```

For Daytona, replace the two Modal variables with `DAYTONA_API_KEY` and set both `MAC_BENCH_BUSINESS_PROVIDER=daytona` and `MAC_BENCH_SOFTWARE_PROVIDER=daytona`. The same client image supports either provider.

Replace the placeholder values with your endpoint and credentials. Run one episode:

```bash
docker compose -f docker/compose.yaml run --rm benchmark \
  evaluate --task-id op26_service_administration_007 \
  --method react_single --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 80 \
  --max-tool-calls 160 --max-seconds 2400 \
  --out outputs/preschool-react
```

Compose mounts `data/` read-only and shares `outputs/` and `.cache/` with the host. Read `outputs/preschool-react/summary.json` locally to inspect scores, then follow its episode paths for traces. [Evaluation](../docs/evaluation.md#read-results) explains the output fields.

## Run Superagent

Build the target that includes Node and the DeepSeek Harness packages:

```bash
docker build --target superagent -f docker/Dockerfile -t mac-bench:superagent .
docker run --rm --env-file .env \
  -v "$PWD/data:/opt/mac-bench/data:ro" \
  -v "$PWD/outputs:/opt/mac-bench/outputs" \
  -v "$PWD/.cache:/opt/mac-bench/.cache" \
  mac-bench:superagent evaluate \
  --task-id op26_service_administration_007 \
  --method superagent --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 3000 \
  --max-tool-calls 9000 --max-seconds 7200 \
  --out outputs/preschool-superagent
```

Use the model and sandbox credentials described above. For GPT-6-Luna through OpenRouter, set `OPENROUTER_API_KEY` in `.env` and add `--method-config configs/baselines/superagent_luna.yaml`.

## Export software build contexts

For an installed software task, export its dependency setup and public workspace:

```bash
mac-bench environment docker-context \
  --task-id flit_packages --out outputs/docker/flit_packages
```

The output includes:

| Directory | Contents |
| --- | --- |
| `base/` | Base image and fixed dependency setup |
| `task/` | Starter source, public materials and task initialization |

Each export includes a README with build commands. These contexts let you inspect or build the software workspace with Docker; the evaluation command above uses the selected cloud provider for execution and scoring.
