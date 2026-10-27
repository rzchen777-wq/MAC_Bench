"""Bounded reads and explicit row edits in existing JSON registers.

Native storage remains a Twenty note/task. The world service orders all its
agent calls under one lock; content versions reject stale editor snapshots.
This is not an atomic transaction against unrelated external application writers.
"""
from copy import deepcopy
from hashlib import sha256
import json

from mac_bench.environments.catalog.entities.errors import CatalogError
from mac_bench.environments.catalog.entities.pagination import paginate
from .compound import CompoundOperation, Steps, StepStopped, native_operations
from .contracts import Tool, ToolError, encoded, object_schema
from .registry import ToolRegistry
from .navigation import resolve


def version(record, content):
    return sha256(encoded({'id': record['id'], 'title': record['title'], 'content': content}).encode()).hexdigest()


def row_key(value):
    if type(value) not in (str, int) or value == '':
        raise ToolError('invalid_row_key', 'Every row key must be a nonempty string or integer; booleans are not row keys.')
    return (type(value).__name__, value)


def select_rows(content, array_path):
    import re
    if not array_path.startswith('/') or re.search(r'~(?![01])', array_path):
        raise ToolError('invalid_array_path', 'array_path must be a JSON Pointer to the existing array, such as /volunteers or /results.')
    try:
        rows = resolve(content, array_path)
    except ToolError:
        raise ToolError('not_a_row_register', 'array_path does not identify an existing array. Use the public recording format; do not rename its fields to rows.') from None
    if not isinstance(rows, list):
        raise ToolError('not_a_row_register', 'array_path must identify an array. Use the public recording format and an explicit array_path.')
    return rows


def decode_rows(record, field, array_path='/rows'):
    try:
        content = json.loads(record.get('bodyV2', {}).get('markdown', ''))
    except (ValueError, TypeError):
        raise ToolError('not_a_row_register', 'Use a JSON object with the array required by the public recording format; read other bodies with the native tool.') from None
    if not isinstance(content, dict):
        raise ToolError('not_a_row_register', 'The existing body must be a JSON object. Initialize it using the public recording format, then specify its array_path.')
    rows = select_rows(content, array_path)
    positions = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or field not in row:
            raise ToolError('missing_row_key', 'Every existing row must be an object containing the supplied key_field.')
        key = row_key(row[field])
        if key in positions:
            raise ToolError('duplicate_row_key', 'Existing keys are not unique. Inspect and reconcile the register before editing.')
        positions[key] = i
    return content, positions


class RegisterRows(CompoundOperation):
    def __init__(self, entity, action, operations):
        self.entity, self.action, self.operations = entity, action, operations

    def execute_agent(self, agent, arguments, call_id):
        steps = Steps(agent, call_id, self.operations)
        identity, field = arguments['record_id'], arguments['key_field']
        array_path = arguments.get('array_path', '/rows')
        steps.ids['record_id'] = identity
        fields = ['id', 'title', 'bodyV2.markdown']
        try:
            record = steps.one(self.entity, identity, fields)
            content, positions = decode_rows(record, field, array_path)
            current_version = version(record, content)
            if self.action == 'read_rows':
                rows = select_rows(content, array_path)
                total_rows = len(rows)
                selected = arguments.get('row_ids')
                if selected is not None:
                    keys = {row_key(k) for k in selected}
                    rows = [r for r in rows if row_key(r[field]) in keys]
                page = paginate(rows, revision=current_version,
                    query=[self.entity, identity, array_path, field, selected], cursor=arguments.get('cursor'),
                    limit=arguments.get('limit', 20), max_bytes=10000)
                return steps.success(title=record['title'], content_version=current_version,
                    total_rows=total_rows, array_path=array_path, content_fields=sorted(content),
                    **page)

            if arguments['expected_version'] != current_version:
                raise ToolError('stale_record_version', 'The register changed since your read. Read fresh rows and use the returned content_version; no edit was made.')
            changed = deepcopy(content)
            changed_array = select_rows(changed, array_path)
            touched = set()
            updated, inserted, deleted = [], [], []
            def claim(value):
                key = row_key(value)
                if key in touched:
                    raise ToolError('overlapping_row_edits', 'A row key may occur only once across updates, insert_rows and delete_ids.')
                touched.add(key)
                return key
            for edit in arguments.get('updates', []):
                key = claim(edit['key'])
                if key not in positions:
                    raise ToolError('row_not_found', 'An updated key is missing. Use an explicit insert_rows entry to create a row.')
                if field in edit['set'] and row_key(edit['set'][field]) != key:
                    raise ToolError('row_key_change', 'An update cannot change its key. Use explicit deletion/insertion if required by the task.')
                changed_array[positions[key]].update(deepcopy(edit['set']))
                updated.append(edit['key'])
            for row in arguments.get('insert_rows', []):
                if field not in row:
                    raise ToolError('missing_row_key', 'Every inserted row must contain key_field.')
                key = claim(row[field])
                if key in positions:
                    raise ToolError('row_already_exists', 'An inserted key already exists. Use updates to edit it.')
                changed_array.append(deepcopy(row))
                inserted.append(row[field])
            removed = set()
            for value in arguments.get('delete_ids', []):
                key = claim(value)
                if key not in positions:
                    raise ToolError('row_not_found', 'A deleted key is missing. No edit was made.')
                removed.add(key)
                deleted.append(value)
            changed_array[:] = [r for r in changed_array if row_key(r[field]) not in removed]
            changed_rows = [r for r in changed_array if row_key(r[field]) in touched]
            if changed != content:
                result = steps.invoke(self.entity, 'updateOne', {'id': identity,
                    'data': {'bodyV2': {'markdown': encoded(changed)}}, 'fields': fields})
                if steps.confirmed_id(result, 'record_id') != identity:
                    raise ToolError('record_readback_mismatch', 'Write returned a different record ID. Inspect receipts and live state.')
                actual = steps.one(self.entity, identity, fields)
                saved, _ = decode_rows(actual, field, array_path)
                if actual['title'] != record['title'] or saved != changed:
                    raise ToolError('record_readback_mismatch', 'Saved register differs from the requested edit. Inspect live state before another write.')
                current_version = version(actual, saved)
            return steps.success(title=record['title'], content_version=current_version, total_rows=len(changed_array), array_path=array_path,
                updated_ids=updated, inserted_ids=inserted, deleted_ids=deleted, changed_rows=changed_rows, changed=changed != content)
        except CatalogError as exc:
            return steps.failure(ToolError('invalid_row_pagination', 'Page cursor no longer matches this register/query, or a row exceeds the page budget. Read again or use the native read and query_result.'))
        except (ToolError, StepStopped) as exc:
            return steps.failure(exc)


def extend(registry):
    operations = native_operations(registry, {'twenty.note', 'twenty.task'})
    tools = list(registry.tools())
    key = {'type': ['string', 'integer'], 'description': 'Exact row-key value; string and integer keys are distinct.'}
    for entity in ('twenty.note', 'twenty.task'):
        for action in ('read_rows', 'patch_rows'):
            needs = {'findOne'} | ({'updateOne'} if action == 'patch_rows' else set())
            if any((entity, op) not in operations for op in needs):
                continue
            base = operations[entity, 'findOne' if action == 'read_rows' else 'updateOne']
            props = {'record_id': {'type': 'string', 'minLength': 1, 'description': 'Exact existing native note/task UUID.'},
                'array_path': {'type': 'string', 'pattern': '^/', 'maxLength': 512, 'default': '/rows',
                    'description': 'JSON Pointer to the existing array required by the public format, e.g. /volunteers, /results, /sessions, /restaurants. Defaults to /rows only; no field is inferred or renamed.'},
                'key_field': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                    'description': 'Explicit field that uniquely identifies every row, e.g. id. No business key is inferred.'}}
            required = ['record_id', 'key_field']
            if action == 'read_rows':
                props.update(row_ids={'type': 'array', 'items': key, 'maxItems': 100, 'uniqueItems': True},
                    cursor={'type': 'string', 'minLength': 1}, limit={'type': 'integer', 'minimum': 1, 'maximum': 100})
            else:
                props.update(expected_version={'type': 'string', 'pattern': '^[0-9a-f]{64}$',
                        'description': 'content_version from a fresh read_rows or successful patch_rows. Stale versions are rejected before writes.'},
                    updates={'type': 'array', 'maxItems': 100, 'items': object_schema({'key': key,
                        'set': {'type': 'object', 'minProperties': 1, 'description': 'Merge these top-level fields into this row; arrays/nested values are replaced, not recursively merged.'}}, ['key','set'])},
                    insert_rows={'type': 'array', 'maxItems': 100, 'items': {'type': 'object', 'minProperties': 1}},
                    delete_ids={'type': 'array', 'maxItems': 100, 'items': key, 'uniqueItems': True})
                required.append('expected_version')
            tools.append(Tool('records.'+entity.split('.')[1]+'.'+action, 'twenty', 'twenty.structured_record', action,
                base.binding, base.scope,
                ('Read selected or paginated rows from an existing JSON register without returning the whole table.' if action == 'read_rows' else
                 'Edit explicit rows in an existing JSON register, preserving omitted rows, fields, title and native relationships.'),
                object_schema(props, required),
                'Returns record_id, title, content_version, row counts and attributed native receipts; read returns items/next_cursor, edit returns changed_rows and affected IDs.',
                action == 'read_rows', RegisterRows(entity, action, operations),
                ('Supports an explicitly selected array of objects with unique keys inside a JSON document. It does not infer fields, outcomes or business decisions.',
                 'Initialize a new register with records.note.create or records.task.create using the public title and actual format, e.g. content={"volunteers":[]}; read/edit with array_path=/volunteers. Preserve the task-defined field names.',
                 'Patch first validates all keys and the content version, then performs at most one native update and reads back the complete saved register.',
                 'Agent calls through this world service are serialized. External application writers are not covered by a native transaction.',
                 'Unknown/partial writes are not retried. Read current rows and receipts; use a new request ID only after resolving the prior outcome.',
                 'Version/cursor values describe live reads; replaying a request returns the old receipt, not refreshed data.'), {}))
    return ToolRegistry(tools, catalog_revision=registry.catalog_revision)
