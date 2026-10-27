<h1 align="center">MAC-Bench</h1>
<p align="center"><strong>Benchmarking Multi-Agent Collaboration via Interconnected Tasks</strong></p>
<p align="center">
<a href="docs/dataset.md">Dataset</a> · <a href="#quickstart">Quickstart</a> ·
<a href="docs/environments.md">Environments</a> · <a href="docs/agents.md">Your agent</a>
</p>

MAC-Bench evaluates multi-agent collaboration in **business operations and software engineering**. Tasks contain interconnected work branches that exchange outputs or share resources. Systems decide how to divide and coordinate the work; outcome verifiers measure partial progress and complete resolution.

MAC-Bench contains **48 test tasks and 6 development tasks across six categories**.

| Example task | Work that must fit together |
| --- | --- |
| Prepare a preschool intake | Resolve applications, reserve suitable places and schedule family inductions with qualified officers across six centres. |
| Complete a Python packaging backend | Read project metadata, select source files, build wheels and source archives, and support installation through standard build interfaces. |

Browse the [task descriptions](data/public_tasks.json) or use `mac-bench tasks show TASK_ID`.

## Quickstart

Use Python 3.11+ on Linux. From the repository root:

```bash
pip install -e '.[modal,agents]'
mac-bench tasks list --split test
mac-bench tasks show op26_service_administration_007
```

Extract the dataset archive into the repository root:

```bash
tar -xzf mac-bench-data.tar.gz
```

Task descriptions, initial resources and evaluator files are ordinary files under `data/tasks/`. See [task data](docs/data.md) for selective downloads and local edits.

Configure your model in `.env` and authenticate Modal:

```bash
cp .env.example .env
modal token new
```

Set `MAC_BENCH_LLM_BASE_URL`, `MAC_BENCH_LLM_API_KEY`, and `MAC_BENCH_LLM_MODEL`.
Then run a single ReAct episode with a 1M-token budget:

```bash
mac-bench evaluate \
  --task-id op26_service_administration_007 \
  --method react_single --repetitions 1 --max-tokens 1000000 \
  --out outputs/react
```

Read `outputs/react/summary.json` for scores and follow its episode paths for trajectories. Add `--dry-run` to inspect the selected task and budget before starting.

See [environment setup](docs/environments.md) for Modal images, software dependency preparation, reset and cleanup. The [Docker guide](docker/README.md) packages the evaluation client with the same Modal runtime.

## Evaluate your system

The evaluator provides the task description, permitted tools and an initialized environment. Agents in the same team share one task environment; separate episodes are isolated. Your system controls delegation, communication and stopping.

[Agent integration](docs/agents.md) describes the interface. ReAct, AOrchestra, Magentic-One and Meta-Team adapters are included; additional baseline dependencies are installed separately.

## Documentation

| Topic | Guide |
| --- | --- |
| Tasks, splits and scores | [Dataset](docs/dataset.md) |
| Install and verify task data | [Data](docs/data.md) |
| Load environments and reset tasks | [Environments](docs/environments.md) |
| Run evaluations and inspect results | [Evaluation](docs/evaluation.md) |
| Connect an agent system | [Agent integration](docs/agents.md) |
| Docker client and software build materials | [Docker](docker/README.md) |

## License

MAC-Bench code is licensed under [MIT](LICENSE). Application and software-project licenses accompany their respective assets; baseline sources retain their upstream licenses.
