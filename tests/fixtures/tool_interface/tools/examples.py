"""Small schema-checked examples; synthetic values never imply business validity."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from mac_bench.construction.realize.tools.contracts import encoded
from mac_bench.construction.realize.tools.navigation import resolve


def example(schema):
    def value(node, ancestry=()):
        if '$ref' in node:
            ref = node['$ref']
            if ref in ancestry:
                raise ValueError('Required recursive input has no finite example')
            return value(resolve(schema, ref), (*ancestry, ref))
        if 'const' in node:
            return deepcopy(node['const'])
        if 'enum' in node:
            return deepcopy(next((x for x in node['enum'] if x not in ('', None)), node['enum'][0]))
        if node.get('examples'):
            return deepcopy(node['examples'][0])
        for union in ('anyOf', 'oneOf'):
            if union in node:
                return value(next((n for n in node[union] if n.get('type') != 'null'), node[union][0]), ancestry)
        kind = node.get('type')
        if isinstance(kind, list):
            kind = next((k for k in kind if k != 'null'), 'null')
        if kind == 'object' or 'properties' in node:
            properties = node.get('properties', {})
            keys = set(node.get('required', []))
            # HRMS server-required fields are useful in a complete example even
            # when a default/fetch/hook can supply them for real calls.
            keys.update(k for k, p in properties.items() if p.get('x-native-constraints', {}).get('required'))
            return {k: value(properties[k], ancestry) for k in sorted(keys)}
        if kind == 'array':
            return [value(node.get('items', {}), ancestry) for _ in range(node.get('minItems', 0))]
        if kind in ('integer', 'number'):
            return max(1, node.get('minimum', 0), node.get('exclusiveMinimum', 0) + 1)
        if kind == 'boolean':
            return False
        if kind == 'null':
            return None
        if kind == 'string':
            text = {'uuid': '00000000-0000-4000-8000-000000000001',
                    'date': '2027-01-01', 'date-time': '2027-01-01T09:00:00Z',
                    'email': 'example@example.invalid'}.get(node.get('format'), 'example')
            return text.ljust(node.get('minLength', 0), 'x')[:node.get('maxLength', 200)]
        return None
    try:
        candidate = value(schema)
        Draft202012Validator(schema).validate(candidate)
        if len(encoded(candidate).encode()) > 2500:
            return []
        return [{'arguments': candidate,
                 'meaning': 'Schema-valid synthetic input. Replace placeholders with native IDs and business values. This is not a validated business transaction or task solution.'}]
    except (ValueError, KeyError, IndexError, RecursionError):
        return []
    except Exception as exc:
        from jsonschema import ValidationError
        if isinstance(exc, ValidationError):
            return []
        raise
