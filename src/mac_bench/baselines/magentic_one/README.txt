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

Install/acquire
pip install -e '.[magentic_one]'
git clone --depth 1 --filter=blob:none --sparse https://github.com/microsoft/autogen.git external/baselines/Magentic-One
git -C external/baselines/Magentic-One sparse-checkout set python/packages/autogen-core python/packages/autogen-agentchat python/packages/autogen-ext
Check out source.revision from the YAML (fetch that revision first if necessary).
The clone is an ignored acquired dependency; source and prompt hashes are recorded.
AutoGen core/agentchat/ext metadata must be version 0.7.5. No embedding service
or cross-task training asset is needed by this adaptation.

Run
PYTHONPATH=src:. .venv/bin/python -m mac_bench.evaluation \
  --world PATH_TO_SEALED_WORLD --method magentic_one \
  --method-config configs/baselines/magentic_one.yaml \
  --out NEW_RESULT_DIRECTORY --repetitions 1
Choose llm.base_url/model_name/api_key_env and limits/context_policy in the YAML.
The independent evaluator owns grading and closes Modal resources after execution.
