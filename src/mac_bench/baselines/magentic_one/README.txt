Magentic-One task-layer adapter
==============================
Entry: mac_bench.baselines.magentic_one.tasks:MagenticOne
Config: configs/baselines/magentic_one.yaml

Uses the official AutoGen MagenticOneGroupChat/Orchestrator from the pinned source
checkout, including native facts/plan generation, structured Progress Ledger,
next-speaker selection, stall detection, facts/plan revision and conversation
reset. It does not reimplement those algorithms. Worker composition is explicitly
adapted to the benchmark's terminal/API worlds using native AssistantAgent.

Engineering team: native FileSurfer, MagenticOneCoderAgent, CodeExecutorAgent
(named FileSurfer, Coder, ComputerTerminal). FileSurfer browses candidate files
through the public terminal transport. Coder writes executable code blocks;
ComputerTerminal executes them without a model. The ledger prompt explains this
handoff: select the terminal after a Coder block, without copying that block into
the dispatch instruction (the shared conversation already contains it).
Operational team: Operator, Analyst, Reviewer.
Operational members have the task's published tools; role boundaries are
instructions, not access-control boundaries. They can issue a batch of tool
calls per turn, up to the configured worker_tool_iterations_per_turn before
returning results to orchestration. All world calls
are serialized over the same task-owned candidate. No host filesystem, browser,
Docker executor, reference implementation or private grading tool is exposed.
Worker/conversation reset changes only AutoGen state, not the shared world.
The scoped native orchestrator repair resets the stall counter on entry to a
new plan, allowing dispatch before judging that plan stalled again. It retains
round counts, native ledgers, speaker selection and all shared budget limits.
No forced dispatch, synthetic progress, or task-specific solving is added.

All model calls cross the common recorded LiteLLM gateway. Coordinator, workers,
JSON-ledger retries, final response and context summaries share one episode budget.
The bridge reports text/function-calling capabilities; JSON ledgers use the native
prompt-and-parser path rather than claiming provider structured-output support.
Unsupported vision or forced-tool/structured-output modes fail explicitly.

Both coordinator and workers use bounded WorkingContext. Native conversations
remain untouched for ledger logic/state export; API histories are incrementally
cached and compacted, resetting the cache on native conversation replacement.
Current public task, Task Ledger, role rules and latest coordination request stay
visible. Old recoverable reads can be abbreviated; other evidence goes into a
rolling summary when needed. Summaries retain original evidence in context_archive
records. Token counts are calibrated estimates, not provider-tokenizer guarantees.

Native runtime errors terminate through AutoGen's ordinary exception path; the
original gateway budget/provider exception is retained and re-raised to run_episode
so independent final-state scoring, receipts and cleanup still run. In-flight
threaded model/summary calls are awaited before cleanup on cancellation.

Native artifacts: method.json, messages.jsonl, ledgers.jsonl, team_state.json on
normal completion. Interrupted runs retain incremental messages, ledgers and the
common raw model/tool/context archives even without a final native state file.
Coordinator self-reported completion is only a termination signal, not a score.

Install and run from the repository root
python scripts/fetch_baselines.py --name Magentic-One
pip install -e '.[modal,agents,magentic_one]'
mac-bench data download --task-id op26_service_administration_007
mac-bench evaluate --task-id op26_service_administration_007 \
  --method magentic_one --repetitions 1 \
  --max-tokens 1000000 --max-model-calls 80 \
  --max-tool-calls 160 --max-seconds 2400 \
  --out outputs/preschool-magentic-one

The fetch script installs the pinned source under .cache/baselines/Magentic-One.
AutoGen core/agentchat/ext use version 0.7.5. Configure Modal and model access as
described in docs/getting-started.md. To change method settings, pass
--method-config with a copy of configs/baselines/magentic_one.yaml.
The evaluator owns grading and closes Modal resources after execution.
