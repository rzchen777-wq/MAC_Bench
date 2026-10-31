<h1 align="center">MAC-Bench</h1>
<p align="center"><strong>Benchmarking Multi-Agent Collaboration via Interconnected Tasks</strong></p>
<p align="center">
📖 <a href="#overview">Overview</a> &nbsp;·&nbsp;
🧩 <a href="#tasks-at-a-glance">Tasks</a> &nbsp;·&nbsp;
🚀 <a href="#quickstart">Quickstart</a> &nbsp;·&nbsp;
🤗 <a href="https://huggingface.co/datasets/sihuichuibuqi/MAC-Bench">Dataset</a><br>
🤖 <a href="#evaluate-your-agent">Evaluate Your Agent</a> &nbsp;·&nbsp;
📚 <a href="#documentation">Documentation</a> &nbsp;·&nbsp;
☁️ <a href="docs/environments.md">Environments</a>
</p>
<p align="center">
<a href="https://github.com/rzchen777-wq/MAC_Bench/actions/workflows/tests.yml"><img src="https://github.com/rzchen777-wq/MAC_Bench/actions/workflows/tests.yml/badge.svg" alt="Offline checks"></a>
<a href="#quickstart"><img src="https://img.shields.io/badge/Python-3.11%2B-6C5CE7" alt="Python 3.11 or later"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-238B78" alt="MIT license"></a>
<a href="docs/tasks.md"><img src="https://img.shields.io/badge/Test_tasks-48-376AB3" alt="48 test tasks"></a>
<a href="docs/tasks.md"><img src="https://img.shields.io/badge/Development_tasks-6-B87924" alt="6 development tasks"></a>
</p>

<a id="overview"></a>

## 📖 Overview

MAC-Bench evaluates multi-agent collaboration through **interconnected tasks** in **business operations** and **software engineering**.

- 🧩 **Interconnected work.** Task components exchange outputs or share resources, so their results need to fit together.
- 🤝 **Your system, your team.** Choose the agents, divide the work and decide how they communicate.
- 📊 **Outcomes that can be checked.** Measure partial progress and complete resolution from application records and software artifacts.

<p align="center">
<img src="docs/assets/overview.png" alt="MAC-Bench task example, benchmark scope, and main results" width="100%">
</p>

<a id="tasks-at-a-glance"></a>

## 🧩 Tasks at a Glance

**Two domains, six categories.** Each category contains eight test tasks and one development task.

| 🏢 Business operations | 💻 Software engineering | 📋 Tasks per category |
| --- | --- | --- |
| Finance | Applications | 8 test + 1 development |
| Service administration | Libraries | 8 test + 1 development |
| Project and content operations | Developer tools | 8 test + 1 development |

### 🏫 Prepare the autumn preschool intake

Reconcile applications, reserve suitable places and arrange family inductions across six centres. The system receives signed evidence, programme capacities, family availability and officer records. Siblings must attend the same centre; each induction must match the placements and an appropriately qualified officer's schedule. Evaluation checks the updated reservations, appointments and handover records together.

<details>
<summary>📄 <strong>Read the full task instructions</strong> · Preschool intake</summary>

Prepare the autumn preschool intake

You work for Brookfield Public Preschool Admissions Office. Six centres need their autumn intake ready, but duplicate applications, decided appeals, withdrawals and unresolved residence evidence have left the provisional roster out of date. Existing place reservations and family appointments may therefore be for the wrong child or programme. Today is 2 August 2027. Complete preparation by 20 August. Appointment times are UTC.

There are 136 application entries for 120 children. The signed evidence supports ninety-six places from eighty-four families; twelve children lack current residence evidence and twelve have withdrawn. A child's place must fit guardian consent, age at term opening and access needs. Siblings need the same centre, although they may need different age-band programmes.

Each eligible family also needs one induction at its receiving centre, with siblings together. The guardian and all placed children must attend that appointment, and the admissions officer must meet the family's language and access-support needs. Place choices, family availability and officer schedules must therefore agree before the centre can receive a usable roster.

Task materials

Use public/materials.json to find the original business evidence by subject or business reference. It links to commission, workforce, centre-and-programme and family dossiers in public/business_materials.md and their Twenty notes. They connect existing evidence; all original source notes remain present and protected.

Twenty notes titled BPPA SOURCE ... contain signed applications, evidence, appeals, programme details and family contacts. Pretix holds free place reservations; Twenty holds induction calendars; HRMS holds officers' qualifications, leave and duties. Use public/records.json and the identity maps to locate records. Full admissions conditions and seven register formats are in public/business_policy.md and public/recording.md.

Required outcomes

- Resolve each child's application using the signed identity, current residence evidence, consent and decided appeals. Correct duplicate handling and retain the supported placement, evidence-deferral or withdrawal outcome, without changing source evidence to make the allocation easier.
- Confirm a suitable free programme place for every eligible child, matching age, guardian choices and access needs. Keep siblings at the same centre while respecting their different age bands, programme capacities and accessible-place limits. Retain compatible Pretix reservations and correct or cancel duplicate, obsolete and wrong-programme holds; deferred or withdrawn children keep no place.
- Arrange one accepted family induction at the receiving centre, with the actual guardian and all placed siblings together. Use an officer with the required language and access-support qualifications, create a visible calendar and submit a matching HRMS duty. Respect centre capacity, travel between centres, leave and existing work rather than creating an extra deferral or waiting list.
- Leave each child with a clear outcome and each centre with a usable incoming roster. Connect placed children to their actual reservation and family induction, and remove those arrangements for deferred or withdrawn children. The records prepare future admission; they do not assert attendance or require fees, payments or refunds.

Make the required changes in the applications and complete the handover under the policy and recording guides. Preserve original evidence and unrelated records. Keep future activities recorded as arrangements rather than completed work. You may organize the work freely.

</details>

### 📦 Complete the release packaging backend

Implement Flit's metadata handling, source selection, wheels, source archives and build interfaces. The workspace includes API declarations, documentation, supporting components and public tests. The components must work together: a source archive must rebuild into an installable wheel. Evaluation tests the resulting packages in a fresh consumer environment.

<details>
<summary>📄 <strong>Read the full task instructions</strong> · Flit backend</summary>

Complete the release packaging backend

Implement the missing release backend in Flit 3.12.0. Application developers
need this backend to turn Python projects into distributable wheels and source
archives, then install and run those deliverables in environments that do not
contain the builder or project checkout. This is the complete flit_core subsystem,
with five responsibilities rather than a single packaging example.

1. Configuration and metadata: read supported legacy and PEP621 pyproject
   metadata; validate options, names, versions, dependencies, optional groups,
   authors, readmes, licenses and entry points. Inspect modules for dynamic
   version/description metadata and handle unsupported or malformed inputs.
2. Source and resources: locate modules/packages in root and src layouts,
   preserve resources, select package files and external data, and honor source
   archive include/exclude patterns without omitting crucial rebuild inputs.
3. Wheels: write installable wheel contents, tags, metadata, license resources,
   entry points and complete RECORD hashes/sizes. Honor normalized permissions
   and SOURCE_DATE_EPOCH reproducibility, preserving binary resources.
4. Source distributions: construct correct tar archives with PKG-INFO and source
   metadata, include the files required to rebuild, and exclude configured files.
   Rebuilding the resulting archive must produce a usable wheel.
5. Build interfaces: expose PEP517/660 requirements, metadata preparation,
   wheel/sdist and editable build hooks; preserve their documented behavior.
   Installable results must support imports, resources and console commands.

TOML parsing, SPDX license data, version normalization and the frontend CLI
are provided. Public APIs, declarations, docstrings and original tests remain.
Choose your own implementation; grading checks artifacts in a fresh offline
consumer with neither flit_core nor the source workspace, not source identity.
Version normalization and configuration exception declarations must remain usable.

Workspace: /work/project. Python: /opt/engineering/bin/python.
Set PYTHONPATH=/work/project/flit_core. Public checks:
/opt/engineering/bin/python -m pytest -q flit_core/tests_core/

</details>

Browse all instructions with `mac-bench tasks list` and `mac-bench tasks show TASK_ID`, or read the [task guide](docs/tasks.md).

<a id="quickstart"></a>

## 🚀 Quickstart

Use **Python 3.11+ on Linux**, a Modal account, and a model endpoint supporting chat completions and tool calls.

### 1. 📦 Install and load the data

```bash
git clone https://github.com/rzchen777-wq/MAC_Bench.git
cd MAC_Bench
pip install -e '.[modal,agents]'
mac-bench data download --task-id op26_service_administration_007
mac-bench tasks show op26_service_administration_007
```

Download the task and its required application images from [Hugging Face](https://huggingface.co/datasets/sihuichuibuqi/MAC-Bench). Shared image layers are downloaded once and reused. The last command displays **Prepare the autumn preschool intake**, the development task used below. See the [installation guide](docs/getting-started.md#2-load-the-task-data) for the full dataset, development split and individual task packages.

### 2. 🔑 Configure Modal and your model

```bash
cp .env.example .env
modal token new
```

Set `MAC_BENCH_LLM_BASE_URL`, `MAC_BENCH_LLM_API_KEY`, and `MAC_BENCH_LLM_MODEL` in `.env`. The evaluator runs locally; Modal hosts the task environment.

### 3. ▶️ Run one episode

```bash
mac-bench evaluate \
  --task-id op26_service_administration_007 \
  --method react_single --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 80 \
  --max-tool-calls 160 --max-seconds 2400 \
  --out outputs/preschool-react
```

The first run prepares the application environment; subsequent runs reuse its image and load independent task states. These are introductory run limits; [evaluation settings](docs/evaluation.md#choose-the-run-budget) explain how to choose them for comparisons.

> [!TIP]
> Add `--dry-run` to preview the selected task, system and budget before starting an episode.

### 4. 📊 Inspect the result

```bash
python examples/read_results.py outputs/preschool-react/summary.json
```

The reader prints **Progress Score (PS)**, **Resolution Rate (RR)**, token usage and the path to each episode's traces. PS measures satisfied task goals; RR measures complete success. [Result examples](docs/evaluation.md#read-results) show how to interpret the output.

<details>
<summary>📊 <strong>See a real evaluation result</strong></summary>

A Meta-Team episode on `op26_service_administration_005` produced the following output. This is a separate run from the tutorial above.

```text
Task: op26_service_administration_005
System: meta_team | Status: completed
Scored episodes: 1/1
PS: 33.33% | RR: 0.00%
Episode 1: PS 33.33% | RR 0.00% | stop budget:tokens
  Tokens: 9,774,113 input + 283,677 output | Tool calls: 415
```

Evaluation completed, with partial progress but no full resolution. The printed episode directory contains the model calls, tool activity and outcome checks.

</details>

```text
outputs/preschool-react/
├── experiment.json      # Task, model, system and budget
├── summary.json         # Scores and episode outcomes
├── episode_1.json       # Result and artifact location
└── attempts/            # Model calls, tool activity and verification results
```

**Next steps:** [Full walkthrough →](docs/getting-started.md) · [Try a software task →](docs/getting-started.md#try-a-software-task)

<a id="evaluate-your-agent"></a>

## 🤖 Evaluate Your Agent

An adapter implements `run(context)`: read the task, call its tools, and return when the team has finished. All agents in a team operate on the same task environment.

```python
# Inside your agent's run(context):
instruction = context.task.instruction
functions = [tool.as_function() for tool in context.task.tools]
# Execute a tool selected by your system:
# result = context.environment.call(tool_name, arguments)
```

Start from [the runnable minimal agent](examples/minimal_agent.py). It connects model tool calls to the environment and lets the evaluator record usage and score the final state:

```bash
python examples/minimal_agent.py \
  --task-id op26_service_administration_007 --out outputs/my-agent
```

The [integration guide](docs/agents.md) walks through the example and explains team execution. Adapters for **ReAct, AOrchestra, Magentic-One and Meta-Team** are included; see [baseline setup](docs/evaluation.md#run-the-included-baselines).

<a id="documentation"></a>

## 📚 Documentation

| I want to… | Start here |
| --- | --- |
| 🚀 Run my first task | [Getting started](docs/getting-started.md) |
| 🧩 Explore tasks and materials | [Tasks](docs/tasks.md) |
| 📊 Run baselines and read results | [Evaluation](docs/evaluation.md) |
| 🤖 Connect my own agent system | [Agent integration](docs/agents.md) |
| ☁️ Prepare and configure environments | [Environments](docs/environments.md) |

🐳 **Prefer Docker?** Use the [containerized evaluation client](docker/README.md) with Modal environments.

🛠️ **Want to contribute?** See [Contributing](CONTRIBUTING.md) for development, testing and issue reports.

<a id="license"></a>

## 📄 License

MAC-Bench code is licensed under [MIT](LICENSE). Application, software-project and baseline assets retain their respective upstream licenses; see [NOTICE](NOTICE).
