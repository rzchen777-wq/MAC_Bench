# Configuration

`baselines/` contains settings for the included systems and optional upstream
source pins. `evaluation/` contains their common evaluation protocol.

For each model connection field, an explicit value in the method's `llm` section
takes precedence over `MAC_BENCH_LLM_*` in `.env`. Use `api_key_env` to name the
variable containing the API key. Pass a configuration file with
`--method-config path/to/config.yaml`; see [Evaluation](../docs/evaluation.md).

Superagent uses `baselines/superagent.yaml` for the standard model connection and
`baselines/superagent_luna.yaml` for GPT-6-Luna through OpenRouter. Install its
Node dependencies as described in [Evaluation](../docs/evaluation.md#run-superagent).
The paper's per-task budgets are listed in `evaluation/task_budgets.json`.
