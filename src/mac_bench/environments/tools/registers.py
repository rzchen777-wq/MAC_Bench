"""Schema-backed task registers with their original titles and JSON bodies."""
from copy import deepcopy
import json
import re

from jsonschema import Draft202012Validator, FormatChecker

from .compound import CompoundOperation, Steps, StepStopped, native_operations
from .contracts import Tool, ToolError, encoded, object_schema, validate
from .record_rows import version
from .registry import ToolRegistry


class RegisterCatalog:
    def __init__(self, specification):
        if specification.get('format') != 'mac_bench.registers.v1':
            raise ValueError('Unsupported public register catalog')
        self.specification = deepcopy(specification)
        self.registers = {}
        for spec in specification['registers']:
            key = spec['id']
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,59}', key) or key in self.registers:
                raise ValueError('Invalid or duplicate register ID')
            if spec['entity'] not in ('twenty.note', 'twenty.task') or not spec['title']:
                raise ValueError('Unsupported register storage')
            Draft202012Validator.check_schema(spec['schema'])
            if spec['schema'].get('type') != 'object':
                raise ValueError('A register must be a JSON object')
            self.registers[key] = spec

    def extend(self, registry):
        operations = native_operations(registry, {'twenty.note', 'twenty.task'})
        tools = list(registry.tools())
        for key, spec in self.registers.items():
            entity = spec['entity']
            for action in ('create', 'read', 'update'):
                needs = {'findOne'} | ({'findMany', 'createOne'} if action == 'create' else
                    {'updateOne'} if action == 'update' else set())
                if any((entity, op) not in operations for op in needs):
                    continue
                base = operations[entity, 'findOne']
                props, required = {}, []
                if action != 'read':
                    props['content'] = deepcopy(spec['schema']); required.append('content')
                if action != 'create':
                    props['record_id'] = {'type': 'string', 'minLength': 1}; required.append('record_id')
                if action == 'update':
                    props['expected_version'] = {'type': 'string', 'minLength': 1}; required.append('expected_version')
                tools.append(Tool('registers.' + key + '.' + action, base.application, entity, action,
                    base.binding, deepcopy(base.scope),
                    action.capitalize() + ' the ' + spec['title'] + ' register. Checks the public JSON format; '
                    'stores supplied decisions only, without performing financial, booking, staffing or calendar actions.',
                    object_schema(props, required),
                    'record_id, exact content, content_version and native receipts. Updates replace supplied content '
                    'only after matching the fresh version; omitted native fields/relations are preserved.',
                    action == 'read', RegisterOperation(spec, action, operations),
                    notes=('The title is fixed by the public recording guide. No hidden answer or business decision is filled.',
                           'Formatting success is not business correctness. Native outcomes must be checked separately.',
                           'Keep originals and source evidence. Do not repeat an unknown create; reconcile the native record first.')))
        return ToolRegistry(tools, catalog_revision=registry.catalog_revision)


class RegisterOperation(CompoundOperation):
    def __init__(self, spec, action, operations):
        self.spec, self.action, self.operations = spec, action, operations

    def execute_agent(self, agent, arguments, call_id):
        spec, entity = self.spec, self.spec['entity']
        steps = Steps(agent, call_id, self.operations)
        fields = ['id', 'title', 'bodyV2.markdown']
        try:
            def title_for(content):
                key = spec.get('title_field')
                if key is None:
                    return spec['title']
                value = content.get(key)
                if not isinstance(value, str) or not value or len(value) > 150:
                    raise ToolError('invalid_register_identity', 'The public title-key field must be a nonempty business ID.')
                return spec['title'].replace('{' + key + '}', value)
            def native_fields(content):
                return {name: content[key] for name, key in spec.get('native_fields', {}).items()}
            if self.action != 'read':
                validate(spec['schema'], arguments['content'])
                errors = list(Draft202012Validator(spec['schema'], format_checker=FormatChecker()).iter_errors(arguments['content']))
                if errors:
                    raise ToolError('invalid_register_format', 'Invalid date/format at ' +
                        '/'.join(map(str, errors[0].path)) + '; follow the public register schema.')
            if self.action == 'create':
                title = title_for(arguments['content'])
                existing = steps.many(entity, {'title': {'eq': title}}, ['edges.node.id', 'edges.node.title'])
                if existing:
                    raise ToolError('register_already_exists', 'This register title already exists. Locate and read it before updating; do not create a duplicate.')
                result = steps.invoke(entity, 'createOne', {'data': {'title': title, **native_fields(arguments['content']),
                    'bodyV2': {'markdown': encoded(arguments['content'])}}, 'fields': fields})
                identity = steps.confirmed_id(result, 'record_id')
            else:
                identity = arguments['record_id']; steps.ids['record_id'] = identity
                record = steps.one(entity, identity, fields)
                try:
                    content = json.loads(record['bodyV2']['markdown'])
                except (KeyError, TypeError, ValueError):
                    raise ToolError('invalid_register_body', 'Read and reconcile the existing body using native operations; it is not a JSON object.') from None
                title = title_for(content)
                if record['title'] != title:
                    raise ToolError('wrong_register', 'This record does not have the selected register title; no edit was made.')
                if self.action == 'read':
                    return steps.success(content=content, title=title, content_version=version(record, content))
                if title_for(arguments['content']) != title:
                    raise ToolError('register_identity_change', 'An update cannot change this register business identity.')
                if version(record, content) != arguments['expected_version']:
                    raise ToolError('stale_record_version', 'The register changed. Read fresh content and reconcile your update; no edit was made.')
                steps.invoke(entity, 'updateOne', {'id': identity,
                    'data': {'bodyV2': {'markdown': encoded(arguments['content'])}, **native_fields(arguments['content'])}, 'fields': fields})
            actual = steps.one(entity, identity, fields)
            content = json.loads(actual['bodyV2']['markdown'])
            if actual['title'] != title or content != arguments['content']:
                raise ToolError('record_readback_mismatch', 'Saved register differs from the request; inspect native state before another write.')
            return steps.success(content=content, title=title, content_version=version(actual, content))
        except (ToolError, StepStopped) as exc:
            return steps.failure(exc)
