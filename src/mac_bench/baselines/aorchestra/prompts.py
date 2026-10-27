"""Translate upstream benchmark assumptions, retaining its decision workflow."""
from types import SimpleNamespace


def translate(text, changes):
    for old, new in changes:
        if old not in text:
            raise ValueError(f'AOrchestra upstream prompt changed: {old!r}')
        text = text.replace(old, new)
    return text


def install_worker_prompts(module, guide):
    # Brace escaping is required because upstream formats the entire template.
    prefix = 'ENVIRONMENT GUIDE\n' + guide.replace('{', '{{').replace('}', '}}') + '\n\n'
    module.GAIA_PROMPT = prefix + translate(module.GAIA_PROMPT, [
        ('Original Question', 'Original Task'),
        ('Use print() in ExecuteCodeAction to see computation results',
         'Use the published application tools and inspect their returned evidence'),
    ])
    module.TERMINALBENCH_PROMPT = prefix + (
        'WORKER TOOL BINDING\nIn your JSON actions, execute uses the published terminal tool\n'
        'with the same command/timeout parameters. All execution is in the shared candidate.\n\n'
    ) + translate(module.TERMINALBENCH_PROMPT, [
        ('If you run out of steps without "finish", your work is lost and marked as timeout.',
         'If you run out of steps without "finish", this worker times out; candidate changes persist.'),
        ('Use this info: repeat what WORKED, avoid what FAILED.',
         'Use this info: continue from what WORKED, avoid what FAILED. Candidate changes persist.'),
        ('Original Question', 'Original Task'),
        ('Your work is LOST if you timeout.', 'Your report may be incomplete if you timeout.'),
        ('what to repeat and what to avoid', 'what to continue and what to avoid'),
    ])


class WorldPrompt:
    def __init__(self, domain, guide):
        from aorchestra.prompts.gaia import GAIAMainAgentPrompt
        from aorchestra.prompts.terminalbench import TerminalBenchPrompt
        self.domain, self.guide = domain, guide
        self.native = TerminalBenchPrompt if domain == 'engineering' else GAIAMainAgentPrompt

    def build_prompt(self, **values):
        values['tools'] = [SimpleNamespace(name=t.name, description=t.description,
                            parameters=t.input_schema) for t in (values.get('tools') or [])]
        text = self.native.build_prompt(**values)
        if self.domain == 'engineering':
            text = translate(text, [
                ('software installation/configuration task', 'software engineering task'),
                ('CRITICAL: CONTAINER LIFECYCLE\n- Each SubAgent runs in a FRESH container - if you delegate_task again, the previous work will be lost\n- When SubAgent reports status="done", use \'submit\' immediately to run tests in that container',
                 'CRITICAL: SHARED CANDIDATE LIFECYCLE\n- All SubAgents work in the same persistent candidate sandbox; delegation does not reset files\n- When reports and checks establish completion, use submit; independent grading is external'),
                ('SubAgent executes shell commands in a Docker container.',
                 'SubAgent executes shell commands in the shared candidate sandbox. execute is an alias for the published terminal tool.'),
                ('[steps to REPEAT]', '[completed steps to KEEP]'),
            ])
        else:
            from aorchestra.prompts.gaia import format_tools_description
            description = format_tools_description(values['tools'])
            text = translate(text, [
                (f'"tools": choose the potential tools from the {description} to complete the subtask: ["tool1", "tool2", "..."],',
                 '"tools": ["published_tool_name"]'),
                ('QUESTION', 'TASK'),
                ('SUFFICIENTLY answer the TASK?', 'SUFFICIENTLY satisfy the public TASK requirements in the live applications?'),
                ('If any subtask returned a valid result with status "done" → Consider using \'complete\'',
                 'If a subtask returned status "done" → Check whether ALL public task requirements are satisfied'),
                ("'complete'", "'submit'"),
                ('"complete"', '"submit"'),
                ('Note: Higher-priced models are generally more capable. Price correlates with model strength.\n\nModel Selection Strategy:\n- Choose cheaper models for simple tasks\n- Choose more capable models for complex reasoning or critical attempts',
                 'This experiment provides one configured backbone model for all agents; delegate using that model.'),
                ('ANSWER FORMAT: requires precise, concise answers (single word, number, or short phrase). Do NOT include explanations in the answer field.',
                 'COMPLETION: application changes must be made through tools. Submission reports completion; prose is not an application operation. Grading is external.'),
                ('"answer": "concise answer"', '"summary": "observed completion and remaining issues"'),
                ("A SPECIFIC, ACTIONABLE subtask (e.g., 'Extract second word from abstract of paper 2211.xxxxx')",
                 'A SPECIFIC, ACTIONABLE subtask with the application operations and checks needed'),
                ('answers the question', 'satisfies the public task requirements'),
                ('to answer the question', 'to satisfy the public task requirements'),
                ('with the answer', 'with a completion summary'),
            ])
        return text + '\n\nENVIRONMENT GUIDE\n' + self.guide
