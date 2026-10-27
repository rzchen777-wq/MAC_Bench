# Dataset

| Domain | Category | Test | Development |
| --- | --- | ---: | ---: |
| Business operations | Finance | 8 | 1 |
| Business operations | Service administration | 8 | 1 |
| Business operations | Project and content operations | 8 | 1 |
| Software engineering | Applications | 8 | 1 |
| Software engineering | Libraries | 8 | 1 |
| Software engineering | Developer tools | 8 | 1 |

## A task instance

Each task includes instructions, initial resources and a permitted interface. Business tasks expose application tools and structured records. Software tasks expose a persistent workspace through a terminal. Agent roles and communication patterns are chosen by the evaluated system.

`data/public_tasks.json` contains browsable descriptions. `data/tasks.json` maps task IDs to `data/tasks/<task_id>/`. That directory contains one current task package:

```text
manifest.json   # File checksums, task identity and environment metadata
public/         # Instructions and resources available to the solver
private/        # Initial-state checks, outcome verifiers and reference materials
environment/    # Business application configuration and initial data
```

The evaluator controls which files and tools agents receive. Outcome checks and reference materials stay outside the solver interface.

## Scores

**Progress Score (PS)** measures the fraction of task goals satisfied. **Resolution Rate (RR)** measures complete task success. Per-episode values are in [0, 1]; multiply by 100 for percentages. The final state and artifacts determine the score. A verbal claim of completion does not change the environment.

Use development tasks for integration and tuning, then freeze system settings before test evaluation. Record the task checksum, model, system settings and budget with every result.
