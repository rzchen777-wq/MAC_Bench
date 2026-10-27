---
name: systematic-debugging
description: >
  Debugging mental model for fixing real-world bugs from GitHub issues.
  Covers: reproduce → locate → fix → verify.
trigger: When working on a bug fix task
---

# Systematic Debugging

## 1. Understand

Read the issue carefully. Identify:
- **Expected** vs **actual** behavior
- Error messages, tracebacks, reproduction steps
- Component/module likely involved

## 2. Reproduce

If the issue mentions a specific test, run it first to confirm the failure:
```
python -m pytest <test_file>::<test_name> -x --tb=short
```
A failing test confirms you're looking at the right place.

## 3. Locate the Root Cause

Read the relevant function/class in full context. Trace the control flow:
- What inputs lead to the buggy behavior?
- Where exactly does the logic go wrong?

Common root causes:
- Missing validation / unhandled edge case
- Incorrect conditional logic
- Wrong variable reference or type mismatch
- Missing import or wrong function signature

## 4. Fix

Make the **minimal change** that fixes the root cause. Verify your edit immediately with `git diff`.

When replacing text through terminal, check that the old text matches exactly one occurrence. If not unique, include more surrounding lines.

## 5. Verify

1. Run the specific failing test — does it pass now?
2. Run a broader scope to check for regressions
3. Review `git diff` one final time — is the patch clean and minimal?
