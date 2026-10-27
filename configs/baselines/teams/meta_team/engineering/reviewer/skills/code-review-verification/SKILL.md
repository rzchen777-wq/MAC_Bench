---
name: code-review-verification
description: >
  Structured checklist for verifying bug fix patches.
  Ensures correctness, minimality, and no regressions.
trigger: When reviewing a developer's bug fix
---

# Code Review Checklist

## 1. Understand the Context
- Read the original issue description (from the dispatch message)
- Understand what a correct fix should look like

## 2. Review the Diff
Check `git diff` and `git diff --stat`:
- [ ] **Minimality**: Only necessary lines changed. No unrelated reformatting.
- [ ] **Correctness**: Change addresses the root cause, not just symptoms.
- [ ] **No syntax errors**: Modified files parse correctly.
- [ ] **No broken imports**: New imports (if any) exist and are correct.
- [ ] **No test modifications**: Test files must not be changed.

## 3. Run Tests
Run the relevant test suite. Passing tests alone aren't enough — also verify the logic.

## 4. Spot-Check Logic
If the diff touches complex logic, read the surrounding context to verify the fix makes sense in its full scope.

## 5. Report
Report to the **planner**:
- **If VALID**: `send_message(to=["planner"], content="[VERIFIED] <what was checked and why it's correct>")`
- **If ISSUES**: `send_message(to=["planner"], content="[ISSUES] <specific problems with file names and line numbers>")`

## Common Pitfalls
- Don't just check if tests pass — verify the logic is actually correct
- Watch for fixes that mask the bug rather than fix the root cause
- Check that edits didn't break adjacent code
