"""Scoped repairs to the pinned native ledger loop, preserving its dispatch."""
from contextlib import contextmanager


EXECUTION_GUIDE = '''
ENGINEERING TEAM EXECUTION CONTRACT
Coder writes executable code; it has no direct terminal or filesystem tools.
ComputerTerminal executes fenced sh/bash/python code and returns actual output;
it is not a coding or planning model. FileSurfer browses existing candidate files.
When Coder supplies an executable code block requiring execution, select
ComputerTerminal next and ask it to execute Coder's latest block. The terminal
already receives that block through the shared conversation; do not copy it
into another message, since that can execute the same script twice.
Do not ask Coder to read or edit directly without executing its proposed code.
Use one manageable executable block per handoff, then return observed results
to Coder for implementation or debugging. Commands may read, edit, and test the
persistent candidate. A proposed script is not an executed change.
'''


@contextmanager
def repaired_orchestrator(*, engineering, event):
    from autogen_agentchat.teams._group_chat._magentic_one import _magentic_one_group_chat as group
    original = group.MagenticOneOrchestrator

    class RepairedOrchestrator(original):
        async def _reenter_outer_loop(self, cancellation_token):
            # A new plan must receive a fresh stall window before it is judged.
            # Do not reset round counts, budget, facts, plan, or candidate data.
            previous = self._n_stalls
            self._n_stalls = 0
            if previous:
                event({'kind': 'stall_window_reset', 'agent_id': 'Orchestrator',
                       'previous_stalls': previous})
            return await super()._reenter_outer_loop(cancellation_token)

        def _get_progress_ledger_prompt(self, *args, **kwargs):
            text = super()._get_progress_ledger_prompt(*args, **kwargs)
            return text + (EXECUTION_GUIDE if engineering else '')

        def _get_task_ledger_plan_prompt(self, *args, **kwargs):
            text = super()._get_task_ledger_plan_prompt(*args, **kwargs)
            return text + (EXECUTION_GUIDE if engineering else '')

        def _get_task_ledger_plan_update_prompt(self, *args, **kwargs):
            text = super()._get_task_ledger_plan_update_prompt(*args, **kwargs)
            return text + (EXECUTION_GUIDE if engineering else '')

    group.MagenticOneOrchestrator = RepairedOrchestrator
    try:
        yield
    finally:
        group.MagenticOneOrchestrator = original
