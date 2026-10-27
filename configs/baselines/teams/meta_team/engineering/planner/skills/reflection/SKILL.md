---
name: reflection
description: "Lead the team through post-task reflection to improve future performance. Use this skill when reflection/evolution is enabled after a task is completed."
---

# Reflection Skill — Chairman's Post-Task Improvement Protocol

## When to Use This Skill

Use this skill after completing a task when reflection is enabled. The system will inform you
in the system context if reflection is enabled.

## How Reflection Works

The system manages reflection **automatically**. You do NOT need to send messages to agents
telling them to reflect — the system broadcasts L1/L2/L3 instructions to all agents directly.

Your job in each phase is to **complete your own reflection**, not to coordinate others.

## Phase 1: Self-Reflection (L1)

The system automatically sends L1 instructions to all agents. Focus on **your own** performance:

- Did you analyze the issue correctly?
- Was your initial delegation effective? Did you provide enough context in the dispatch?
- Did you recruit the right agents? Was the reviewer needed?
- How could you improve your dispatch quality?

Use `update_prompt_patch` or `update_skill` to record improvements.
Call `skip_l1_reflection` when done.

**Do NOT send messages to other agents during L1.** The system has already given them instructions.

## Phase 2: Cross-Agent Reflection (L2)

The system automatically advances to L2 when all agents finish L1.

1. **Update teammate profiles**: Use `update_teammate_profile` to record your assessment of each agent you worked with.
2. **Update correlations**: Use `update_correlation` to record pairwise collaboration insights.
3. Call `skip_l2_reflection` when done.

Only discuss with a teammate if there was a concrete collaboration problem. Skip discussion by default.

## Phase 3: Structural Reflection (L3)

The system automatically advances to L3 when all agents finish L2.

Only propose changes if the task failed due to structural problems:
- Use `view_reflection()` to see team improvement suggestions
- Use `view_current_config(target_file)` before any change
- Use `propose_reflection(target_file, content, reason)` to propose changes
- Call `apply_reflection(confirmation="apply")` to apply all proposals
- Or call `skip_reflection(reason)` if no structural changes needed

## Important

- Keep reflections concise and actionable
- Focus on patterns that will help in future tasks, not just this one
- The evolution system will persist improvements across task runs
- Don't spend more than 2-3 tool calls per phase — efficiency matters
