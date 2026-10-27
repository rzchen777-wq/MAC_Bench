"""Readable operation contracts derived from the exact executable schema."""
from copy import deepcopy

from .contracts import encoded, validate, ToolError
from .navigation import resolve


def resolved_input(schema, *, nodes=90, depth=5):
    """Expand references within a budget; every deferred constraint stays linked."""
    remaining = [nodes]
    complete = [True]

    def render(node, path='', ancestry=(), level=0):
        if not isinstance(node, dict):
            return deepcopy(node)
        if '$ref' in node:
            ref = node['$ref']
            if ref in ancestry:
                complete[0] = False
                return {'details': {'section': 'input', 'path': ref.removeprefix('#')}, 'recursive': True}
            base = render(resolve(schema, ref), ref.removeprefix('#'), (*ancestry, ref), level)
            siblings = {k: v for k, v in node.items() if k != '$ref'}
            return {'allOf': [base, render(siblings, path, ancestry, level)]} if siblings else base
        if remaining[0] <= 0 or level > depth:
            complete[0] = False
            return {k: deepcopy(node[k]) for k in ('type', 'required') if k in node} | {
                'complete': False, 'details': {'section': 'input', 'path': path}}
        remaining[0] -= 1
        out = {}
        for key, value in node.items():
            if key == '$defs':
                continue
            location = path + '/' + key.replace('~', '~0').replace('/', '~1')
            if key == 'properties':
                required = set(node.get('required', []))
                ordered = sorted(value.items(), key=lambda item: (
                    item[0] not in required,
                    isinstance(item[1], dict) and ('$ref' in item[1] or item[1].get('type') in ('object', 'array')), item[0]))
                out[key] = {name: render(child, location + '/' + name.replace('~', '~0').replace('/', '~1'),
                                         ancestry, level + 1) for name, child in ordered}
            elif key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
                out[key] = [render(child, location + '/' + str(i), ancestry, level + 1)
                            for i, child in enumerate(value)]
            elif key in ('items', 'additionalProperties', 'not', 'if', 'then', 'else', 'contains', 'propertyNames'):
                out[key] = render(value, location, ancestry, level + 1)
            elif len(encoded(value).encode()) > 1000:
                complete[0] = False
                out[key + '_details'] = {'section': 'input', 'path': location, 'complete': False}
            else:
                out[key] = deepcopy(value)
        return out

    result = render(schema)
    return {'schema': result, 'complete': complete[0],
            'raw': {'section': 'raw_input', 'path': ''},
            'note': 'Expanded display of the executable contract. Explicit details links retain deferred fields and constraints; the original schema validates calls.'}


def projections(tool, *, requested=None):
    output = tool.native_contract.get('output', {})
    defaults = output.get('default_fields', [])
    if not defaults:
        return {}
    preferred = ('id', 'name', 'name.firstName', 'name.lastName', 'emails.primaryEmail',
                 'companyId', 'targetCompanyId', 'targetPersonId', 'noteId', 'taskId',
                 'title', 'bodyV2.markdown', 'startsAt', 'endsAt',
                 'location', 'isCanceled', 'status', 'personId', 'calendarEventId',
                 'pageInfo.hasNextPage', 'pageInfo.endCursor')
    selected = [p for p in defaults if p in preferred or
                (p.startswith('edges.node.') and p.removeprefix('edges.node.') in preferred)]
    if not selected:
        selected = defaults[:6]
    result = {'example_fields': selected,
            'paths_start_at': 'The native root result, without a data prefix.',
            'collection': any(p.startswith('edges.node.') for p in defaults),
            'details': {'section': 'output', 'path': '/default_fields'}}
    if requested is not None:
        result['issues'] = projection_issues(output, requested)
        result['correction'] = ('Use complete leaf-field paths from the native root. Correct the arguments '
                                'and use a fresh request_id; repeating an old request_id returns its saved receipt.')
    return result


def projection_issues(output, requested):
    """Explain paths using the captured schema, without reflecting supplied values."""
    from .adapters.twenty import PRIVATE_ACCOUNT_FIELDS
    types = output.get('types', {})

    def native_type(name):
        return types.get(name.replace('[', '').replace(']', '').replace('!', ''), {})

    def leaves(node, prefix='', depth=0):
        found = []
        if depth > 4:
            return found
        for name, field in node.get('fields', {}).items():
            if name in PRIVATE_ACCOUNT_FIELDS or field.get('selection_arguments'):
                continue
            path = prefix + name
            child = native_type(field['type'])
            if child.get('kind') in ('scalar', 'enum'):
                found.append(path)
            elif child.get('kind') == 'object':
                found.extend(leaves(child, path + '.', depth + 1))
            if len(found) >= 8:
                break
        return found[:8]

    issues = []
    if not isinstance(requested, list):
        return issues
    for index, path in enumerate(requested):
        if not isinstance(path, str):
            continue
        segments = path.split('.')
        if any(name in PRIVATE_ACCOUNT_FIELDS for name in segments):
            continue
        node = native_type(output.get('root_type', ''))
        prefix = ''
        reason = None
        for offset, name in enumerate(segments):
            field = node.get('fields', {}).get(name)
            if not field:
                reason = 'Unknown field at this level of the output schema.'
                break
            if field.get('selection_arguments'):
                reason = 'This field requires native selection arguments; consult the output contract.'
                break
            prefix += name + '.'
            node = native_type(field['type'])
            if offset == len(segments) - 1 and node.get('kind') == 'object':
                reason = 'Object-valued fields require a leaf-field selection.'
            elif offset < len(segments) - 1 and node.get('kind') in ('scalar', 'enum'):
                reason = 'A scalar field cannot contain another field.'
                break
        if reason:
            issues.append({'argument_path': '/fields/' + str(index), 'reason': reason,
                           'valid_leaf_paths': leaves(node, prefix)})
        if len(issues) >= 4:
            break
    return issues


def call_example(tool):
    examples = tool.native_contract.get('examples', [])
    if not examples:
        return None
    arguments = deepcopy(examples[0]['arguments'])
    # Add useful, fictional values only when the real schema accepts them.
    options = []
    if 'data' in tool.input_schema.get('properties', {}) and isinstance(arguments.get('data', {}), dict):
        options.append({**arguments, 'data': {**arguments.get('data', {}),
                        'title': 'Museum workshop note', 'bodyV2': {'markdown': 'Fictional documentation example.'}}})
    if 'filter' in tool.input_schema.get('properties', {}):
        options.append({**arguments, 'filter': {'id': {'eq': '00000000-0000-4000-8000-000000000001'}}})
        options.append({**arguments, 'filter': {'title': {'ilike': '%Museum%'}}})
    if tool.entity == 'pretix.order' and tool.operation.startswith('change'):
        options.append({**arguments, 'body': {'patch_positions': [
            {'position': 1, 'body': {'subevent': 2}}]}})
    for option in options:
        try:
            validate(tool.input_schema, option)
            arguments = option
        except ToolError:
            pass
    selection = projections(tool).get('example_fields')
    if selection and 'fields' in tool.input_schema.get('properties', {}):
        arguments['fields'] = selection
    try:
        validate(tool.input_schema, arguments)
    except ToolError:
        return None
    return {'tool_name': tool.name, 'arguments': arguments, 'request_id': 'fictional-example-001'}


def request_card(tool):
    # Reduce expansion rather than dropping unknown optional/conditional rules.
    for size, depth in ((90, 5), (45, 4), (20, 3), (8, 2)):
        result = resolved_input(tool.input_schema, nodes=size, depth=depth)
        if len(encoded(result).encode()) <= 11000:
            return result
    return {'complete': False, 'raw': {'section': 'raw_input', 'path': ''},
            'required': tool.input_schema.get('required', []),
            'parameters': list(tool.input_schema.get('properties', {})),
            'note': 'This operation has a large contract. Read the exact linked input sections; no rules were removed from validation.'}
