"""Strict generation schemas and immutable verifier packages."""
import json
from copy import deepcopy
from hashlib import sha256

from jsonschema import Draft202012Validator

from mac_bench.environments.verification.errors import VerificationError
from mac_bench.environments.verification.snapshot import make_layout


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode()).hexdigest()


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties, 'required': list(properties) if required is None else required,
            'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1}
STRINGS = {'type': 'array', 'items': TEXT, 'uniqueItems': True}
EVIDENCE = {'type': 'array', 'minItems': 1, 'items': obj({'section': TEXT, 'quote': TEXT})}
CLAUSE = obj({'id': TEXT, 'kind': {'enum': ['result', 'preservation', 'process', 'semantic']},
              'requirement': TEXT, 'evidence': EVIDENCE, 'allowed_choices': {'type': 'string'}})
GOAL = obj({'id': TEXT, 'description': TEXT,
            'clause_ids': {**STRINGS, 'minItems': 1}})
EXTRACTION = obj({'clauses': {'type': 'array', 'minItems': 1, 'items': CLAUSE},
                  'goals': {'type': 'array', 'minItems': 1, 'items': GOAL},
                  'global_clause_ids': STRINGS,
                  'context': {'type': 'array', 'items': obj({'role': {'enum': ['setup', 'interpretation']},
                              'statement': TEXT, 'evidence': EVIDENCE})},
                  'ambiguities': {'type': 'array', 'items': TEXT}})
BINDING = obj({'status': {'enum': ['bound', 'unsupported']},
               'collections': STRINGS, 'interpretation': TEXT, 'reason': {'type': 'string'}})
ASSERTION = {'oneOf': [obj({'mode': {'const': 'no_rows'}}),
                       obj({'mode': {'const': 'scalar'},
                            'operator': {'enum': ['eq', 'le', 'ge']},
                            'expected': {'type': 'number'}})]}
QUERIES = obj({'checks': {'type': 'array', 'minItems': 1, 'maxItems': 20,
                         'items': obj({'id': TEXT, 'description': TEXT, 'sql': TEXT,
                                       'assertion': ASSERTION})}})


def schema_check(value, schema):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        raise VerificationError('; '.join(str(list(e.path)) + ': ' + e.message for e in errors[:5]))


def validate_extraction(result, source):
    schema_check(result, EXTRACTION)
    ids = [x['id'] for x in result['clauses']]
    if len(ids) != len(set(ids)):
        raise VerificationError('Duplicate requirement ID')
    validate_goals(result)
    texts = {s['id']: s['text'] for s in source}
    invalid = []
    for clause in result['clauses'] + result['context']:
        for evidence in clause['evidence']:
            if evidence['section'] not in texts or evidence['quote'] not in texts[evidence['section']]:
                invalid.append({'item': clause.get('id', clause.get('statement')), 'evidence': evidence,
                                'source_section_text': texts.get(evidence['section'])})
    if invalid:
        raise VerificationError('Evidence must copy an exact contiguous source excerpt; do not paraphrase or concatenate. '
                                'Invalid citations: ' + json.dumps(invalid, ensure_ascii=False))


def validate_goals(extraction):
    """Keep outcome accounting independent of generated SQL fragmentation."""
    clauses = {c['id']: c for c in extraction['clauses']}
    goals = extraction['goals']
    if len({g['id'] for g in goals}) != len(goals):
        raise VerificationError('Duplicate goal ID')
    references = [c for g in goals for c in g['clause_ids']]
    globals_ = set(extraction['global_clause_ids'])
    if set(references) | globals_ != set(clauses):
        raise VerificationError('Every clause must belong to a goal or global constraints; unknown references are forbidden')
    if set(references) & globals_:
        raise VerificationError('Global constraints must not duplicate goal clause references')
    for goal in goals:
        if all(clauses[c]['kind'] == 'preservation' for c in goal['clause_ids']):
            raise VerificationError('A goal needs a business outcome, not only preservation credit')
    for cid, clause in clauses.items():
        if clause['kind'] == 'result' and cid in globals_:
            raise VerificationError('Result clauses must contribute to a business goal')
        if clause['kind'] != 'preservation' and references.count(cid) > 1:
            raise VerificationError('An outcome clause cannot earn credit in multiple goals')


def validate_binding(binding, sources, identities=None):
    schema_check(binding, BINDING)
    if not set(binding['collections']) <= sources.keys():
        raise VerificationError('Unknown collections: ' + repr(sorted(set(binding['collections']) - sources.keys()))
                                + '. Choose only collection keys: ' + repr(sorted(sources)))
    if binding['status'] == 'bound' and (not binding['collections'] or binding['reason']):
        raise VerificationError('Bound requirement needs sources and no unresolved reason')
    if binding['status'] == 'unsupported' and not binding['reason']:
        raise VerificationError('Unsupported requirement needs an explicit reason')


def package(source, initial, identities, sources, extraction, compiled, gaps, *, facts=None, links=None):
    body = deepcopy({'format': 'business-verifier/sql-v4', 'source': source,
                     'initial': initial, 'identities': identities, 'sources': sources,
                     'layout': make_layout(sources), 'facts': facts or {}, 'links': links or [],
                     'requirements': extraction, 'checks': compiled, 'gaps': gaps,
                     'status': 'ready' if not gaps and not extraction['ambiguities'] else 'needs_resolution'})
    result = {**body, 'revision': digest(body)}
    verify_package(result)
    return result


def verify_package(contract):
    if contract.get('format') != 'business-verifier/sql-v4':
        raise VerificationError('Unsupported verifier format; regenerate legacy packages with explicit business goals')
    if digest({k:v for k,v in contract.items() if k != 'revision'}) != contract.get('revision'):
        raise VerificationError('Verifier package integrity mismatch')
    validate_extraction(contract['requirements'], contract['source'])
    if contract['layout'] != make_layout(contract['sources']):
        raise VerificationError('Frozen projection schema mismatch')
    ids = [row['id'] for row in contract['checks']]
    if len(ids) != len(set(ids)):
        raise VerificationError('Duplicate compiled check ID')
    covered = {row['clause'] for row in contract['checks']}
    gaps = {row['clause'] for row in contract['gaps']}
    expected = {row['id'] for row in contract['requirements']['clauses']}
    if covered | gaps != expected or covered & gaps:
        raise VerificationError('Every requirement needs checks OR an explicit gap')
    status = 'needs_resolution' if contract['gaps'] or contract['requirements']['ambiguities'] else 'ready'
    if contract['status'] != status:
        raise VerificationError('Incorrect package readiness')
    for check in contract['checks']:
        schema_check({k: check[k] for k in ('id', 'description', 'sql', 'assertion')}, QUERIES['properties']['checks']['items'])
        validate_binding(check['binding'], contract['sources'])
        if check['binding']['status'] != 'bound':
            raise VerificationError('Compiled check requires a supported binding')
