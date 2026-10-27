# Meta-Team baseline

The task-layer adapter is `mac_bench.baselines.meta_team.tasks.MetaTeam`.
It runs the pinned upstream Runner/RunManager on the task's public tool interface,
using the common recorded model gateway and shared episode budget.

- Configuration: `configs/baselines/meta_team.yaml`; frozen Hard test variants
  are in `configs/baselines/hard/`.
- Business and software use separately reviewed initial team pools.
- Development exposes owner-controlled aggregate feedback after sealing the
  candidate; accepted native reflection assets form the next team version.
- Test uses a hash-verified frozen pool, with reflection and cross-task updates
  disabled. Private grader/reference assets stay with the task owner.
- Source, adapter, model, team and budget identities are recorded with episodes.

Read [the development/freeze guide](../../../../docs/meta_team_development.md)
and [the evaluation guide](../../../../docs/evaluation.md) before running.
This is the retained task/episode implementation; the former stream/work-order
entrypoints are not part of MAC-Bench.
