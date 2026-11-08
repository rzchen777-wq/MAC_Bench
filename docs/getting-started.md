# Getting started

[Home](../README.md) · **Getting started** · [Tasks](tasks.md) · [Evaluation](evaluation.md) · [Agent integration](agents.md) · [Environments](environments.md)

This walkthrough runs one agent on **Prepare the autumn preschool intake**. You will load the task, configure its environment, run an episode and inspect the scores and interaction history.

## 1. Install the evaluator

Use Python 3.11+ on Linux. Clone the repository and create a Python environment:

```bash
git clone https://github.com/rzchen777-wq/MAC_Bench.git
cd MAC_Bench
python -m venv .venv
source .venv/bin/activate
pip install -e '.[modal,agents]'
mac-bench tasks list --split dev
```

The task index is included with the code. The six development tasks are:

| Task ID | Category |
| --- | --- |
| `op26_business_finance_002` | Finance |
| `op26_service_administration_007` | Service administration |
| `op26_project_content_operations_001` | Project and content operations |
| `paperless_workflows` | Applications |
| `marshmallow_schemas` | Libraries |
| `flit_packages` | Developer tools |

## 2. Load the task data

Download the preschool task from [Hugging Face](https://huggingface.co/datasets/sihuichuibuqi/MAC-Bench):

```bash
mac-bench data download --task-id op26_service_administration_007
mac-bench tasks show op26_service_administration_007
```

This installs the task into `data/tasks/` and its required application images into `data/environments/`. Downloads use the Hugging Face client with parallel transfers and local caching; rerunning the command reuses completed files. Public downloads do not require a token. The task description begins:

> You work for Brookfield Public Preschool Admissions Office. Six centres need their autumn intake ready, but duplicate applications, decided appeals, withdrawals and unresolved residence evidence have left the provisional roster out of date.

The complete description explains how placements, family appointments and officer duties must agree. [Tasks](tasks.md) describes these materials and provides the full instructions.

<details>
<summary><strong>Other splits, pinned releases and offline packages</strong></summary>

```bash
mac-bench data download --split dev    # Six development tasks
mac-bench data download --split test   # 48 test tasks
mac-bench data download                # All 54 tasks
```

The dataset repository and revision are selected in `data/distribution.json`; the default revision is `main`. Each download resolves the selected branch, tag or commit to one commit, so packages and environment files come from the same snapshot. Use `--repo-id OWNER/MAC-Bench` or `--revision BRANCH_TAG_OR_COMMIT` to select another location or revision. `--max-workers` controls parallel downloads (default: 4).

Environment files are stored directly under `data/`, without a second full copy in the download cache. Compressed task packages are cached under `.cache/dataset/packages/`; installed tasks are verified and reused on subsequent downloads. The resolved commit is recorded in `.cache/dataset/release.json`.

To update an existing installation to the release selected by your code checkout, run:

```bash
mac-bench data download --overwrite
mac-bench data verify
```

`--overwrite` replaces the selected task folders after the downloaded packages pass verification. Omit it to preserve local task edits. Shared environment files are reused when their checksums match. Updates also remove superseded image files tracked by the downloader. `data verify` checks task contents and required application images, hashing each shared blob once.

For an independently downloaded software-task package:

```bash
mac-bench data install --task-id flit_packages --archive flit_packages.tar.gz
```

Business tasks also need the OCI files listed for them in `downloads.json`. To transfer an installed dataset to an offline machine, copy `data/tasks/` and `data/environments/` together. An HTTP mirror can be selected with `--url URL_TO_DOWNLOADS_JSON`. If your network cannot reach the Xet transfer service, prefix the download command with `HF_HUB_DISABLE_XET=1` to use the Hub client's HTTP transport.

</details>

## 3. Configure the model and Modal

```bash
cp .env.example .env
modal token new
```

Fill in the following fields in `.env` with your chat-completions endpoint and a model that supports tool calls:

```dotenv
MAC_BENCH_LLM_BASE_URL=https://your-provider.example/v1
MAC_BENCH_LLM_API_KEY=your-api-key
MAC_BENCH_LLM_MODEL=your-model-name
```

The provider URL above is a placeholder for your endpoint. Modal authentication is saved by `modal token new`. Your agent and the evaluator run locally, while task applications run in a Modal sandbox.

## 4. Preview the run

```bash
mac-bench evaluate \
  --task-id op26_service_administration_007 \
  --method react_single --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 80 \
  --max-tool-calls 160 --max-seconds 2400 \
  --out outputs/preschool-react --dry-run
```

The preview reports the task, method and limits without creating an environment. Its budget section is:

```json
"budget": {
  "max_tokens": 1000000,
  "max_model_calls": 80,
  "max_tool_calls": 160,
  "max_seconds": 2400
}
```

To check environment loading separately, run:

```bash
mac-bench environment check \
  --task-id op26_service_administration_007 \
  --out outputs/preschool-environment
```

This starts the environment, verifies the initial records, resets it, checks again and closes it. In `report.json`, `initial.matches: true` and `reset.matches: true` indicate that both checks passed. The check uses Modal compute, with no model calls.

## 5. Run and inspect the episode

Run the command from step 4 again without `--dry-run`. The evaluator prepares the environment, lets ReAct act through its tools, then checks the final application state and saves the result.

```bash
python examples/read_results.py outputs/preschool-react/summary.json
```

The output identifies the task and system, shows PS/RR and token usage, and points to the recorded episode. Open `evaluation.json` there to inspect outcome checks, `model_calls/` for model requests and responses, and `events.jsonl` for the execution history. [Evaluation](evaluation.md#read-results) explains the fields and includes a recorded example.

For another run, choose a new output directory. Within one run, `--repetitions 3` starts three independent episodes with the same settings.

## Try a software task

Use the same installation and model configuration for Flit:

```bash
mac-bench data download --task-id flit_packages
mac-bench tasks show flit_packages
mac-bench environment prepare --task-id flit_packages
mac-bench evaluate --task-id flit_packages \
  --method react_single --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 80 \
  --max-tool-calls 160 --max-seconds 2400 \
  --out outputs/flit-react
python examples/read_results.py outputs/flit-react/summary.json
```

The agent receives a persistent source workspace and terminal tools. Evaluation checks the resulting implementation and artifacts in a separate grading environment.

Next, [connect your own agent](agents.md), [run a multi-agent baseline](evaluation.md#run-the-included-baselines), or [configure environment resources](environments.md).
