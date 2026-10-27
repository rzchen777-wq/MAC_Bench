"""Recover native JSON actions without executing malformed model output."""
import json

from jsonschema import validate


def action_messages(prompt, contracts, *, coordinator):
    role = ('You are the coordinator. Delegate work with delegate_task; only submit '
            'when ready. Application tools listed in the task belong to workers.'
            if coordinator else 'You are a worker. Use one assigned action per response.')
    return [{'role': 'system', 'content': role + '\n'
        'Return exactly one JSON object with action and params fields. '
        'An optional memory field records concise observations. '
        'These are text JSON actions, not provider function calls or DSML/XML. '
        'Do not output code fences or a separate essay. Keep each action manageable; '
        'inspect and edit files incrementally rather than returning a whole project.\n'
        'Allowed actions and parameter schemas:\n' + json.dumps(contracts)},
        {'role': 'user', 'content': prompt}]


async def complete_action(complete, messages, contracts, *, event, attempts=3):
    for attempt in range(attempts):
        response = await complete(messages)
        try:
            decision = json.loads(response.content)
            if not isinstance(decision, dict):
                raise ValueError('Expected one JSON object')
            action = decision.get('action')
            if not isinstance(action, str) or action not in contracts:
                raise ValueError('Use one of: ' + ', '.join(contracts))
            if not isinstance(decision.get('params'), dict):
                raise ValueError('params must be an object')
            validate(decision['params'], contracts[action])
            if response.tool_calls:
                raise ValueError('Use text JSON, not provider tool calls')
            return response
        except (ValueError, TypeError) as exc:
            error = exc
        except Exception as exc:
            from jsonschema import ValidationError
            if not isinstance(exc, ValidationError):
                raise
            error = ValueError(exc.message)
        if attempt == attempts - 1:
            raise error
        event({'kind': 'protocol_recovery', 'attempt': attempt + 1,
               'protocol': 'aorchestra_json_action'})
        # Provider-specific markup is not replayed as a fake assistant tool call.
        messages.append({'role': 'user', 'content':
            'The previous response was rejected; no action was executed. '
            + str(error)[:1000] + '\nReturn the required JSON action object. '
            'Use the allowed actions in the system message.'})
