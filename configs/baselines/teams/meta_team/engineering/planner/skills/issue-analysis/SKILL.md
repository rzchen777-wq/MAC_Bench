---
name: issue-analysis
description: >
  Framework for analyzing GitHub issues and writing effective dispatch
  instructions that save the developer significant exploration time.
trigger: When analyzing a new issue to plan a bug fix
---

# Issue Analysis & Dispatch Planning

## Analyze the Issue

Extract from the problem statement:
1. **Symptom**: What user-visible behavior is wrong?
2. **Expected behavior**: What should happen instead?
3. **Error details**: Traceback, error message, or log output?
4. **Affected component**: File paths, module names, function names mentioned
5. **Bug type**: Type error? Logic error? Missing validation? Regression?

## Hypothesize Root Cause

Map the symptom to likely causes:
- **TypeError / AttributeError** → wrong type, missing attribute, None handling
- **Logic error** → conditional logic, unhandled edge case
- **Import / ModuleNotFoundError** → wrong import path, missing dependency
- **Regression** → recent change broke something that used to work

## Write the Dispatch

A good dispatch saves the developer 5-10 exploration steps. Be specific:

```
ISSUE SUMMARY: <1-2 sentences>
ROOT CAUSE HYPOTHESIS: <your analysis of what's wrong and where>
SUGGESTED APPROACH:
1. Look at <specific file/module>
2. The fix likely involves <specific change>
FILES TO EXAMINE: <file paths with line hints if possible>
TEST COMMAND: <specific pytest command>
```

**"Look at lib/ansible/modules/mount.py around line 200"** is worth far more than **"find the bug"**.
