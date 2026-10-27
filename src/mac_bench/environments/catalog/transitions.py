"""Declarative operation effects used by the native sequence planner.

An adapter registers lifecycle semantics independently of any task. Guards
are JSON Schemas over the current projected record; effects either assign a
contract literal (e.g. a lifecycle state) or bind a requested target value.
A projection effect is a symbolic prediction, never native readback evidence.
"""
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from mac_bench.environments.catalog.fields import (
    canonical_record,
    canonical_value,
)
from mac_bench.environments.catalog.payloads import payload_value
from mac_bench.environments.errors import CompileFailure


@dataclass(frozen=True)
class ProducedRecord:
    entity: str
    values: Mapping[str, Any]
    response_identity: Any


@dataclass(frozen=True)
class AffectedRecords:
    """Existing records selected by symbolic identity, with deterministic effects.

    identities is evaluated against {id,before,values} of the initiating record
    and returns one identity or a list. Effects additionally see `record`, the
    selected record before the operation. All selections/effects use the same
    pre-state, so declaration order cannot change an operation's meaning.
    """
    entity: str
    identities: Any
    precondition: Mapping[str, Any]
    effects: Mapping[str, Any]


@dataclass(frozen=True)
class TransitionContract:
    precondition: Mapping[str, Any]
    target_fields: frozenset[str]
    fixed_effects: Mapping[str, Any] = field(default_factory=dict)
    produces: Mapping[str, ProducedRecord] = field(default_factory=dict)
    affected: Mapping[str, AffectedRecords] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, specification):
        required = {'precondition', 'target_fields', 'fixed_effects', 'produces', 'affected'}
        if (not isinstance(specification, dict) or set(specification) != required
                or not isinstance(specification['precondition'], dict)
                or not isinstance(specification['target_fields'], list)
                or any(not isinstance(name, str) or not name for name in specification['target_fields'])
                or len(set(specification['target_fields'])) != len(specification['target_fields'])
                or not isinstance(specification['fixed_effects'], dict)):
            raise CompileFailure('lifecycle_effect_contract_invalid')
        def records(key, record_type, keys):
            specs = specification[key]
            if not isinstance(specs, dict):
                raise CompileFailure('lifecycle_record_effects_require_mapping')
            result = {}
            for name, spec in specs.items():
                if (not isinstance(name, str) or not name or not isinstance(spec, dict)
                        or set(spec) != keys or not isinstance(spec['entity'], str) or not spec['entity']):
                    raise CompileFailure('lifecycle_record_effect_invalid')
                result[name] = record_type(**deepcopy(spec))
            return result
        return cls(deepcopy(specification['precondition']), frozenset(specification['target_fields']),
                   deepcopy(specification['fixed_effects']),
                   records('produces', ProducedRecord, {'entity', 'values', 'response_identity'}),
                   records('affected', AffectedRecords, {'entity', 'identities', 'precondition', 'effects'}))

    def validate(self, fields, writes):
        Draft202012Validator.check_schema(dict(self.precondition))
        if not self.precondition or not set(self.precondition.get('properties', {})) <= {'id', *fields}:
            raise CompileFailure('transition_requires_valid_record_precondition')
        if set(self.fixed_effects) & self.target_fields:
            raise CompileFailure('transition_effect_declared_twice')
        if set(self.fixed_effects) | self.target_fields != set(writes):
            raise CompileFailure('transition_effect_footprint_mismatch')
        if not writes and not self.produces and not self.affected:
            raise CompileFailure('transition_without_observable_effect')

    def propose(self, before, target, *, fields):
        if next(Draft202012Validator(symbolic_precondition(fields, dict(self.precondition))).iter_errors(before), None):
            return None
        return {**{name: deepcopy(target[name]) for name in self.target_fields},
                **deepcopy(dict(self.fixed_effects))}

    def describe(self):
        return {'precondition': deepcopy(dict(self.precondition)),
                'target_fields': sorted(self.target_fields), 'fixed_effects': deepcopy(dict(self.fixed_effects)),
                'produces': {name: asdict(spec) for name, spec in self.produces.items()},
                'affected': {name: asdict(spec) for name, spec in self.affected.items()}}

    def affected_records(self, state, before, values, *, parameters=None):
        arguments = {'id': before['id'], 'before': before, 'values': values, 'parameters': parameters or {}}
        index = {row['id']: (population, state.populations[population], row)
                 for population, rows in state.records.items() for row in rows}
        result = {}
        for spec in self.affected.values():
            identities = payload_value(spec.identities, arguments)
            identities = identities if isinstance(identities, list) else [identities]
            if (any(not isinstance(identity, str) or not identity for identity in identities)
                    or len(set(identities)) != len(identities)):
                raise CompileFailure('lifecycle_affected_identities_invalid')
            for identity in identities:
                if identity == before['id'] or identity not in index or index[identity][1] != spec.entity:
                    raise CompileFailure('lifecycle_affected_record_not_bound')
                population, _, row = index[identity]
                if next(Draft202012Validator(symbolic_precondition(state.catalog[spec.entity]['fields'], dict(spec.precondition))).iter_errors(row), None):
                    return None
                effects = payload_value(dict(spec.effects), {**arguments, 'record': row})
                patch = result.setdefault(identity, {'population': population, 'entity': spec.entity,
                                                      'before': deepcopy(row), 'values': {}})
                if set(patch['values']) & effects.keys():
                    raise CompileFailure('lifecycle_affected_field_declared_twice')
                patch['values'].update(effects)
        return result

    def created_records(self, operation, before, values, populations, *, fields, parameters=None):
        result = {}
        arguments = {'id': before['id'], 'before': before, 'values': values, 'parameters': parameters or {}}
        canonical_arguments = {'id': before['id'], 'before': canonical_record(fields, before),
                               'parameters': parameters or {}, 'values': {name: canonical_value(fields[name], value)
                                          for name, value in values.items()}}
        for name, spec in self.produces.items():
            candidates = [population for population, entity in populations.items() if entity == spec.entity]
            if len(candidates) != 1:
                raise CompileFailure(f'lifecycle_created_population_ambiguous:{operation.name}:{name}')
            token = json.dumps([operation.entity, operation.name, name, canonical_arguments], sort_keys=True, separators=(',', ':'))
            symbolic = 'effect/' + sha256(token.encode()).hexdigest()
            result[name] = {'population': candidates[0], 'entity': spec.entity,
                            'record': {'id': symbolic, **payload_value(dict(spec.values), arguments)}}
        return result


def symbolic_precondition(fields, schema):
    from copy import deepcopy

    from mac_bench.environments.catalog.fields import symbolic_schema

    def convert(field, native):
        if 'codec' in field:
            return symbolic_schema({**field, 'schema': native})
        if field.get('kind') == 'reference':
            if 'schema' in field:
                return symbolic_schema(field)
            return {'type': 'string', 'minLength': 1}
        result = deepcopy(native)
        if field.get('kind') in {'array', 'set'}:
            child = field.get('items')
            if child is None and field.get('target'):
                child = {'kind': 'reference'}
            if child is not None and isinstance(result.get('items'), dict):
                result['items'] = convert(child, result['items'])
        elif field.get('kind') == 'object':
            for name, child in field['properties'].items():
                if name in result.get('properties', {}):
                    result['properties'][name] = convert(child, result['properties'][name])
            if (isinstance(field.get('additional_properties'), dict)
                    and isinstance(result.get('additionalProperties'), dict)):
                result['additionalProperties'] = convert(field['additional_properties'], result['additionalProperties'])
        return result

    result = deepcopy(schema)
    for name, native in result.get('properties', {}).items():
        result['properties'][name] = convert(fields.get(name, {}), native)
    return result
