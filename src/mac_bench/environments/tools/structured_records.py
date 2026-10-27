"""Structured JSON documents over existing native notes/tasks, with no decisions."""
from copy import deepcopy
import json
from datetime import datetime

from .compound import CompoundOperation, Steps, StepStopped, native_operations
from .contracts import Tool, ToolError, encoded, object_schema
from .registry import ToolRegistry


class StructuredRecord(CompoundOperation):
    def __init__(self, entity, action, operations):
        self.entity, self.action, self.operations = entity, action, operations

    def execute_agent(self, agent, arguments, call_id):
        steps = Steps(agent, call_id, self.operations)
        fields = ['id', 'title', 'bodyV2.markdown']
        if self.entity == 'twenty.task':
            fields += ['status', 'dueAt']
        record_id = arguments.get('record_id')
        if record_id:
            steps.ids['record_id'] = record_id
        try:
            if self.action != 'create':
                record = steps.one(self.entity, record_id, fields)
                if self.action == 'read':
                    try:
                        content = json.loads(record.get('bodyV2', {}).get('markdown', ''))
                    except (ValueError, TypeError):
                        raise ToolError('not_a_json_record', 'This record has no JSON object body. Use the native note/task read operation.') from None
                    if not isinstance(content, dict):
                        raise ToolError('not_a_json_record', 'This record body is not a JSON object. Use the native read operation.')
                    return steps.success(title=record['title'], content=content,
                        **{k: record.get(k) for k in ('status', 'dueAt') if k in fields})
            # Content is an object, not a pre-encoded string. No business fields,
            # discriminators, totals, titles or record links are inferred.
            data = {'title': arguments['title'], 'bodyV2': {'markdown': encoded(arguments['content'])}}
            if self.entity == 'twenty.task':
                data.update({k: deepcopy(arguments[k]) for k in ('status', 'dueAt') if k in arguments})
            result = steps.invoke(self.entity, 'createOne' if self.action == 'create' else 'updateOne',
                {'data': data, 'fields': fields, **({'id': record_id} if record_id else {})})
            steps.confirmed_id(result, 'record_id')
            actual = steps.one(self.entity, steps.ids['record_id'], fields)
            def same(key, value):
                saved = actual.get(key)
                if key == 'dueAt' and saved and value:
                    return datetime.fromisoformat(saved.replace('Z', '+00:00')) == datetime.fromisoformat(value.replace('Z', '+00:00'))
                return saved == value
            if any(not same(k, v) for k, v in data.items()):
                raise ToolError('record_readback_mismatch', 'The saved record does not match the supplied content. Inspect live state.')
            return steps.success(title=actual['title'], content=arguments['content'],
                **{k: actual.get(k) for k in ('status', 'dueAt') if k in fields})
        except (ToolError, StepStopped) as exc:
            return steps.failure(exc)


def extend(registry):
    operations = native_operations(registry, {'twenty.note', 'twenty.task'})
    tools = list(registry.tools())
    for entity in ('twenty.note', 'twenty.task'):
        for action, native_action in [('create', 'createOne'), ('read', 'findOne'), ('update', 'updateOne')]:
            if any((entity, op) not in operations for op in {'findOne', native_action}):
                continue
            base = operations[entity, native_action]
            props, required = {}, []
            if action != 'create':
                props['record_id'] = {'type': 'string', 'minLength': 1, 'description': 'Exact native UUID, not a business alias.'}
                required.append('record_id')
            if action != 'read':
                props.update(title={'type': 'string', 'minLength': 1, 'maxLength': 255},
                    content={'type': 'object', 'description': 'JSON object with the business fields required by the public recording specification. Pass the object directly; do not JSON-encode it.'})
                required += ['title', 'content']
                if entity == 'twenty.task':
                    native = operations[entity, native_action].input_schema
                    # Copy actual captured native constraints rather than inventing
                    # a second enum/date contract for the task application.
                    from .navigation import resolve
                    data = native.get('properties', {}).get('data', {})
                    if '$ref' in data:
                        data = resolve(native, data['$ref'])
                    if 'anyOf' in data:
                        data = data['anyOf'][0]
                        if '$ref' in data:
                            data = resolve(native, data['$ref'])
                    for key in ('status', 'dueAt'):
                        if key in data.get('properties', {}):
                            props[key] = deepcopy(data['properties'][key])
            name = 'records.' + entity.split('.')[1] + '.' + action
            tools.append(Tool(name, 'twenty', 'twenty.structured_record', action, base.binding, base.scope,
                f'{action.capitalize()} a structured JSON {entity.split(".")[1]} in Twenty. '
                'The content object is stored in bodyV2.markdown; supplied fields are not business approval or execution.',
                object_schema(props, required), 'data contains record_id, title, decoded content and actual native step receipts.',
                action == 'read', StructuredRecord(entity, action, operations),
                ('Public recording requirements still determine titles, fields, identifiers and necessary links.',
                 'No registration, refund, staffing, accounting or other application action is performed.',
                 'Update replaces title and the complete JSON content. Other native fields/relationships are preserved unless explicitly supplied.',
                 'Read/write success does not establish business correctness. Unknown writes are not retried; inspect returned IDs and live state.'),
                {'output': {'record_id': 'Actual native UUID', 'content': 'Decoded JSON object', 'steps': 'Attributed native receipts'}}))
    from .record_rows import extend as extend_rows
    return extend_rows(ToolRegistry(tools, catalog_revision=registry.catalog_revision))
