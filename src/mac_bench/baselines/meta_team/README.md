# Meta-Team baseline

The adapter runs Meta-Team's pinned Runner and RunManager through MAC-Bench's
public task tools. Agents share one task environment, recorded model calls and
an episode budget.

- [Configuration](../../../../configs/baselines/meta_team.yaml) selects the
  initial team pool for business or software tasks.
- Test evaluation uses the configured pool with reflection and cross-task
  updates disabled.
- Episode records include the team, source revision, model and budget.

See [Evaluation](../../../../docs/evaluation.md#run-the-included-baselines) for
installation and run commands.
