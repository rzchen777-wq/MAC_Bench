"""DRF structural constraints; native serializer hooks stay server-authoritative."""
from copy import deepcopy


def schema_for(types, root, *, writing):
    definitions = {}

    def reference(name, *, partial=False):
        name = name.removeprefix('schema/')
        # A captured nested serializer may be constructed with partial=True.
        # Keep a separate projection so its use in an ordinary create body
        # does not lose required fields through the shared definition cache.
        partial = writing and partial
        key = name + '::partial' if partial else name
        if key not in definitions:
            definitions[key] = {}
            declaration = types[name]
            properties, required = {}, []
            conditional = {rule.get('field') for rule in declaration.get('field_rules', []) if rule.get('conditions')}
            for field, spec in declaration.get('fields', {}).items():
                if spec.get('read_only' if writing else 'write_only'):
                    continue
                properties[field] = field_schema(spec)
                if writing and not partial and spec.get('required') and field not in conditional:
                    required.append(field)
            definitions[key] = {'type': 'object', 'properties': properties, 'required': required,
                                 'additionalProperties': False if writing and declaration.get('contract_kind') == 'fixed' else True,
                                 'x-native-contract-kind': declaration.get('contract_kind'),
                                 'x-native-field-rules': declaration.get('field_rules', [])}
        return {'$ref': '#/$defs/' + key.replace('~', '~0').replace('/', '~1')}

    def field_schema(spec):
        kind = spec.get('type', '')
        if spec.get('schema'):
            result = reference(spec['schema'], partial=spec.get('partial') is True)
            if spec.get('many') or kind == 'ListSerializer':
                result = {'type': 'array', 'items': result}
        elif kind in {'IntegerField', 'PrimaryKeyRelatedField'}:
            result = {'type': 'integer'}
        elif kind == 'FloatField':
            result = {'type': 'number'}
        elif kind in {'DecimalField', 'ItemTaxRateField'}:
            result = {'type': ['string', 'number']}
        elif kind == 'BooleanField':
            result = {'type': 'boolean'}
        elif kind in {'ListField', 'ManyRelatedField', 'MultipleChoiceField'}:
            result = {'type': 'array', 'items': field_schema(spec.get('child') or spec.get('child_relation') or {})}
        elif kind == 'I18nField':
            result = {'anyOf': [{'type': 'string'}, {'type': 'object', 'additionalProperties': {'type': 'string'}}]}
        elif kind in {'CharField', 'EmailField', 'URLField', 'SlugRelatedField', 'DateField', 'DateTimeField',
                      'TimeField', 'CountryField', 'CompatibleCountryField', 'TimeZoneField'}:
            result = {'type': 'string'}
            if writing and spec.get('allow_blank') is False:
                result['minLength'] = 1
        else:
            # JSON/custom fields do not acquire guessed wire types. Their full
            # native declaration is retained and native serializer validates it.
            result = {'x-native-field': deepcopy(spec)}
        if spec.get('choices') and kind == 'ChoiceField':
            choices = spec['choices']
            result = {'enum': list(choices) if isinstance(choices, dict) else
                      [v[0] if isinstance(v, (list, tuple)) else v for v in choices]}
        if result.get('type') in ('integer', 'number'):
            for source, target in [('min_value', 'minimum'), ('max_value', 'maximum')]:
                if isinstance(spec.get(source), (int, float)):
                    result[target] = spec[source]
        for source, target in [('min_length', 'minLength'), ('max_length', 'maxLength')]:
            if result.get('type') == 'string' and isinstance(spec.get(source), int):
                result[target] = spec[source]
        # DRF's allow_null constrains deserialization, not whether a read-only
        # relation exists. Guest orders and unseated positions legitimately
        # serialize absent relations as None. Pretix's compatibility country
        # field similarly emits None for an unset address (source='*').
        read_nullable = not writing and (
            kind == 'CompatibleCountryField'
            or spec.get('read_only') and (
                kind in {'SlugRelatedField', 'PrimaryKeyRelatedField'}
                or spec.get('schema') and not spec.get('many') and kind != 'ListSerializer'
            )
        )
        if spec.get('allow_null') or read_nullable:
            result = {'anyOf': [result, {'type': 'null'}]}
        return result

    result = reference(root)
    return {**result, '$defs': definitions}
