# Engineering Team

## Mission

Complete the public software task in the shared candidate repository. Deliver correct, task-scoped changes and verify them with relevant public tests.

## Environment

- **Working directory**: `/work/project` (the project repository)
- **Each `terminal` runs in a fresh subshell** — `cd` does not persist. Always: `cd /work/project && ...`
- **Use non-interactive commands** — use `git --no-pager` and disable progress displays when needed.
- Follow the public task's runtime and test instructions.

## Hard Constraints

- **NEVER** `git checkout --` — discards all changes
- **NEVER** `git commit` — evaluation submits the final candidate source and artifacts
- **NEVER** modify test files — only fix source code
- **NEVER** use interactive tools (vi, nano, less, more) — they will hang
