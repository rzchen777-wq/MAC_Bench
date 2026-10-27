"""Public, typed record forms backed exclusively by registered app operations.

Forms serialize administrative records, not task solutions. All native reads and
writes retain actor attribution, request deduplication and saved receipts.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re

from jsonschema import Draft202012Validator, FormatChecker

from .contracts import Tool, ToolError, encoded, object_schema, validate
from .registry import ToolRegistry


FORMAT = 'mac_bench.record_forms.v1'


def _positions(value, path):
    """Yield editable slots for a bounded field path, optionally through arrays."""
    parts = path.strip('/').split('/')
    def walk(node, remaining):
        key = remaining[0]
        keys = range(len(node)) if key == '*' and isinstance(node, list) else [key]
        for key in keys:
            if isinstance(node, dict) and key not in node:
                continue
            if len(remaining) == 1:
                yield node, key
            else:
                yield from walk(node[key], remaining[1:])
    yield from walk(value, parts)


class FormCatalog:
    def __init__(self, specification, directory):
        if specification.get('format') != FORMAT or not isinstance(specification.get('forms'), list):
            raise ValueError('Unsupported record-form specification')
        self.forms, self.directory = {}, deepcopy(directory)
        for form in specification['forms']:
            key = form['id']
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', key) or key in self.forms:
                raise ValueError('Invalid or duplicate record-form ID')
            if form['entity'] not in {'twenty.note', 'twenty.task'}:
                raise ValueError('Only administrative note/task forms are supported')
            schema = form['schema']
            Draft202012Validator.check_schema(schema)
            if schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
                raise ValueError('Forms require explicit object fields')
            fields = set(schema['properties'])
            if fields != set(form['body_fields']) | set(form['native_fields'].values()):
                raise ValueError('Each form field must have an explicit storage mapping')
            if set(form['body_fields']) & set(form['constants']):
                raise ValueError('Form fields cannot override record discriminators')
            if not set(form['native_fields']) <= {'title', 'status', 'dueAt'}:
                raise ValueError('Unsupported native record field')
            if not isinstance(form['constants'].get('kind'), str):
                raise ValueError('An explicit record discriminator is required')
            if form.get('company_link') not in (None, *form['body_fields']):
                raise ValueError('Company link must refer to a form field')
            if form.get('company_link') and not any(
                    ref['path'] == '/' + form['company_link'] and ref['entity'] == 'twenty.company'
                    and ref.get('business_id') for ref in form.get('references', [])):
                raise ValueError('Company links require an explicit public business reference')
            descriptions = form.get('operation_descriptions')
            if descriptions is not None and (
                    not isinstance(descriptions, dict) or set(descriptions) != {'create', 'read', 'update'}
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 2000
                           for value in descriptions.values())):
                raise ValueError('Operation descriptions must explicitly describe create, read and update')
            self.forms[key] = deepcopy(form)

    def extend(self, registry):
        tools = list(registry.tools())
        for form in self.forms.values():
            for operation in ('create', 'read', 'update'):
                handler = FormOperation(self, form, operation, registry)
                # A form grants no capabilities beyond the actor's native role.
                if not handler.available:
                    continue
                schema = deepcopy(form['schema']) if operation != 'read' else object_schema()
                if operation != 'create':
                    schema['properties']['record_id'] = {'type': 'string', 'minLength': 1,
                        'description': 'Native Twenty record ID, returned by create or a live read.'}
                    schema['required'] = [*schema.get('required', []), 'record_id']
                base = handler.operations[(form['entity'], 'createOne')]
                example = deepcopy(form.get('example', {})) if operation != 'read' else {}
                if operation != 'create':
                    example['record_id'] = '00000000-0000-4000-8000-000000000005'
                description = form.get('operation_descriptions', {}).get(operation,
                    f'{operation.capitalize()} {form["label"]} using typed fields in the native application. '
                    'This records supplied decisions; it does not perform related business work.')
                if operation == 'update':
                    description += ' Update replaces the complete form; supply every required field.'
                tools.append(Tool('records.' + form['id'] + '.' + operation, 'twenty',
                    'twenty.record_form', operation, base.binding, base.scope,
                    description,
                    schema, 'data contains record_id, form values and actor-scoped native step receipts. '
                    'partial/unknown require inspection; the record may already exist.',
                    operation == 'read', handler,
                    ('Fields and reference meanings come from public/record_forms.json.',
                     'No task solution, availability, authorization or budget is inferred.',
                     'Writes are not atomic across record/link operations. Inspect partial/unknown receipts.',
                     'A new request_id is a new action; it can create a second record. '
                     'Reuse the original request_id only to retrieve its receipt.'),
                    {'record_form': form, 'examples': [{'arguments': example}] if example else [], 'output': {
                        'record_id': 'Actual native ID', 'values': 'Typed current record fields',
                        'steps': 'Each actual native invocation and its call_id/status'}}))
        return ToolRegistry(tools, catalog_revision=registry.catalog_revision)


class _Stopped(Exception):
    def __init__(self, receipt):
        self.receipt = receipt


class FormOperation:
    def __init__(self, catalog, form, operation, registry):
        self.catalog, self.form, self.operation = catalog, form, operation
        required = {(form['entity'], x) for x in ('findOne', 'createOne', 'updateOne')}
        for ref in form.get('references', []):
            if ref.get('live', True):
                required.add((ref['entity'], 'read' if ref['entity'].startswith('hrms.') else 'findOne'))
        if form.get('company_link'):
            required.update(('twenty.note_target', x) for x in ('findMany', 'createOne', 'deleteOne'))
        self.operations = {}
        for key in required:
            matches = [t for t in registry.tools() if (t.entity, t.operation) == key]
            if len(matches) == 1:
                self.operations[key] = matches[0]
        self.available = len(self.operations) == len(required)

    def __call__(self, connection, arguments):
        raise RuntimeError('Record forms must execute through an attributed agent session')

    def execute_agent(self, agent, arguments, call_id):
        form, steps, wrote = self.form, [], False
        record_id = arguments.get('record_id')
        existing_body = {}

        def invoke(entity, operation, args):
            nonlocal wrote
            tool = self.operations[entity, operation]
            # Parent receipt identity determines private child request IDs. A
            # repeated parent call never re-enters this function or repeats writes.
            request = 'form-' + sha256((call_id + ':' + str(len(steps))).encode()).hexdigest()
            result = agent.call_tool(tool.name, args, request)
            step = {'tool': tool.name, 'call_id': result['call_id'], 'status': result['status'],
                    'read_only': tool.read_only}
            steps.append(step)
            if not tool.read_only and result['status'] != 'rejected':
                wrote = True
            if result['status'] != 'succeeded':
                raise _Stopped(result)
            if not result.get('data_complete', True):
                result, _ = agent.world.journal.receipt(agent.world.world_id, agent.actor,
                                                       agent.role.name, result['call_id'])
            return result['data']

        def read(entity, identity):
            if entity.startswith('hrms.'):
                return invoke(entity, 'read', {'name': identity})
            fields = ['id', 'title', 'bodyV2.markdown'] if entity == form['entity'] else ['id']
            if entity == form['entity']:
                fields += [k for k in form['native_fields'] if k != 'title']
            return invoke(entity, 'findOne', {'filter': {'id': {'eq': identity}}, 'fields': fields})

        def values_from(record):
            nonlocal existing_body
            if not isinstance(record, dict):
                raise ToolError('record_not_found', 'The native record does not exist.')
            try:
                text = record['bodyV2']['markdown'].strip()
                if text.startswith('```'):
                    match = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL)
                    if not match:
                        raise ValueError()
                    text = match[1]
                body = json.loads(text)
                if not isinstance(body, dict) or any(body.get(k) != v for k, v in form['constants'].items()):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ToolError('record_type_mismatch', 'This is not a record of the requested form type; no write was made.') from None
            existing_body = body
            return {**{k: body[k] for k in form['body_fields'] if k in body},
                    **{field: record.get(native) for native, field in form['native_fields'].items()}}

        try:
            if self.operation != 'create':
                existing = read(form['entity'], record_id)
                current = values_from(existing)
                if self.operation == 'read':
                    return {'status': 'succeeded', 'data': {'record_id': record_id,
                        'record_type': form['id'], 'values': current, 'steps': steps}, 'error': None}
            values = {k: deepcopy(v) for k, v in arguments.items() if k != 'record_id'}
            validate(form['schema'], values)
            errors = list(Draft202012Validator(form['schema'], format_checker=FormatChecker()).iter_errors(values))
            if errors:
                raise ToolError('invalid_arguments', 'Invalid format at ' + '.'.join(map(str, errors[0].path)))
            company_ids = {}
            for ref in form.get('references', []):
                for node, key in _positions(values, ref['path']):
                    value = node[key]
                    matches = [r for r in self.catalog.directory if r['entity'] == ref['entity']
                               and value in r.get('business_ids', [])]
                    if len(matches) > 1:
                        raise ToolError('ambiguous_reference', ref['path'] + ': public reference is ambiguous; use an exact locator.')
                    if ref.get('business_id') and not matches:
                        raise ToolError('unknown_reference', ref['path'] + ': use an exact business ID from public/records.json.')
                    native = matches[0]['identity'][ref.get('identity_field', 'id')] if matches else value
                    if ref.get('live', True) and not read(ref['entity'], native):
                        raise ToolError('reference_not_found', ref['path'] + ': native record does not exist in the stated application/entity.')
                    if ref.get('business_id'):
                        if ref['path'] == '/' + form.get('company_link', ''):
                            company_ids[value] = native
                    else:
                        node[key] = native
            links = []
            if form.get('company_link') and record_id:
                cursor = None
                while True:
                    args = {'filter': {'noteId': {'eq': record_id}}, 'first': 100,
                            'fields': ['edges.node.id', 'edges.node.targetCompanyId',
                                       'pageInfo.hasNextPage', 'pageInfo.endCursor']}
                    if cursor:
                        args['after'] = cursor
                    page = invoke('twenty.note_target', 'findMany', args)
                    links.extend(e['node'] for e in page['edges'])
                    if not page.get('pageInfo', {}).get('hasNextPage'):
                        break
                    next_cursor = page['pageInfo'].get('endCursor')
                    if not next_cursor or next_cursor == cursor or len(links) > 1000:
                        raise ToolError('invalid_native_pagination', 'Inspect the native record links; pagination did not advance.')
                    cursor = next_cursor
            body = {**existing_body, **form['constants'], **{k: values[k] for k in form['body_fields']}}
            data = {native: values[field] for native, field in form['native_fields'].items()}
            data['bodyV2'] = {'markdown': encoded(body)}
            fields = ['id', 'title', 'bodyV2.markdown', *[k for k in form['native_fields'] if k != 'title']]
            result = invoke(form['entity'], 'createOne' if self.operation == 'create' else 'updateOne',
                            {'data': data, 'fields': fields, **({'id': record_id} if record_id else {})})
            if not isinstance(result, dict) or not result.get('id'):
                raise ToolError('incomplete_native_response', 'The record write has no confirmed ID. Inspect native state before any further write.', outcome='unknown')
            record_id = result['id']
            if form.get('company_link'):
                company = company_ids[values[form['company_link']]]
                for link in links:
                    if link.get('targetCompanyId') and link['targetCompanyId'] != company:
                        invoke('twenty.note_target', 'deleteOne', {'id': link['id'], 'fields': ['id']})
                if not any(x.get('targetCompanyId') == company for x in links):
                    invoke('twenty.note_target', 'createOne', {'data': {'noteId': record_id, 'targetCompanyId': company},
                                                            'fields': ['id', 'noteId', 'targetCompanyId']})
            return {'status': 'succeeded', 'data': {'record_id': record_id, 'record_type': form['id'],
                    'values': values, 'steps': steps}, 'error': None}
        except _Stopped as exc:
            status = ('unknown' if exc.receipt['status'] in {'unknown', 'accepted', 'error'} else
                      'partial' if wrote else 'rejected')
            return {'status': status, 'data': {'record_id': record_id, 'steps': steps},
                    'error': {'code': 'record_form_incomplete',
                              'message': 'Inspect the saved step receipts and live record. No write is automatically retried.',
                              'native_status': exc.receipt['status'],
                              'native_error': exc.receipt.get('error')}}
        except ToolError as exc:
            return {'status': 'unknown' if exc.outcome != 'not_started' else 'partial' if wrote else 'rejected',
                    'data': {'record_id': record_id, 'steps': steps},
                    'error': {'code': exc.code, 'message': exc.message}}
