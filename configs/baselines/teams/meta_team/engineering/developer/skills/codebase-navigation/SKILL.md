---
name: codebase-navigation
description: >
  Efficient search strategies for navigating large codebases
  to quickly locate code relevant to a bug fix.
trigger: When exploring an unfamiliar codebase to find relevant code
---

# Codebase Navigation

## Search Strategies

Pick the strategy that matches your situation:

| Situation | Approach |
|-----------|----------|
| Have an error message or keyword | `grep -rn 'keyword' --include='*.py' \| head -20` |
| Know a function/class name | `grep -rn 'def func_name\|class ClassName' --include='*.py'` |
| Know part of a file path | `find /work/project -path '*module*' -name '*.py' \| head -10` |
| Need to trace imports | `grep -rn 'from module import\|import module' --include='*.py'` |
| Looking for relevant tests | `find /work/project -name 'test_*.py' -path '*module*'` |

## Tips for Large Codebases

- **Narrow your grep scope** — search within a subdirectory, not the entire repo
- **Use `-l`** when you just need filenames, not line matches
- **Always pipe through `head`** to avoid flooding output
- **Check git log** — `git log --oneline -10 -- path/to/file.py` reveals recent change context
