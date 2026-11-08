"""Task outcomes and grader execution failures remain distinct."""
import pytest
from mac_bench.environments.engineering.product.verifier import score_product


@pytest.fixture
def contract():
    return {'goals': [{'id': 'a', 'requirement_ids': ['ra']}, {'id': 'b', 'requirement_ids': ['rb']}],
            'requirements': [{'id': 'ra', 'check_ids': ['one', 'two']},
                             {'id': 'rb', 'check_ids': ['three']}],
            'checks': [{'id': c, 'role': 'regression' if c == 'retained' else 'target'}
                       for c in ('one', 'two', 'three', 'retained')]}



def execution(**values):
    return {'status': 'completed', 'checks': [
        {'id': k, 'status': values.get(k, 'passed')} for k in ('one', 'two', 'three', 'retained')]}



def test_goals_require_all_required_checks_and_regression(contract):
    partial = score_product(contract, execution(two='failed'))
    assert partial['gc'] == .5 and partial['task_success'] is False
    retained_failure = score_product(contract, execution(retained='failed'))
    assert retained_failure['gc'] == 1 and retained_failure['task_success'] is False
    assert score_product(contract, execution())['task_success'] is True



@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'extra', 'harness'])
def test_bad_harness_is_not_goal_failure(contract, kind):
    raw = execution()
    if kind == 'missing':
        raw['checks'].pop()
    elif kind == 'duplicate':
        raw['checks'].append(raw['checks'][0])
    elif kind == 'extra':
        raw['checks'].append({'id': 'unfrozen', 'status': 'passed'})
    else:
        raw['checks'][0]['status'] = 'harness_error'
    score = score_product(contract, raw)
    assert score['status'] == 'verifier_error'
    assert score['task_success'] is None and score['gc'] is None



def test_candidate_collection_exception_fails_goal_without_mutating_raw_checks(contract):
    raw = execution(one='harness_error')
    raw['checks'][0]['details'] = ('ERROR collecting tests/test_parser.py\n'
        '/work/project/src/graphql/parser.py:10: in parse\n'
        "E   AttributeError: 'int' object has no attribute 'start'\n")
    score = score_product(contract, raw)
    assert score['status'] == 'failed' and score['gc'] == .5
    assert score['regression_passed'] is True
    assert score['checks'][0]['classification'] == 'candidate_python_exception_during_test_collection'
    assert raw['checks'][0]['status'] == 'harness_error'



def test_collection_dependency_or_private_harness_failure_stays_unscored(contract):
    for details in ('ERROR collecting tests/test_parser.py\n'
                    '/work/grader/native/tests/test_parser.py:10: in parse\n'
                    'E   AttributeError: broken private fixture\n',
                    'ERROR collecting tests/test_parser.py\n'
                    '/work/project/src/graphql/parser.py:10: in parse\n'
                    "E   ModuleNotFoundError: No module named 'missing_dependency'\n"):
        raw = execution(one='harness_error')
        raw['checks'][0]['details'] = details
        assert score_product(contract, raw)['status'] == 'verifier_error'



@pytest.mark.parametrize('name', ['GraphQLSyntaxError', 'graphql.error.syntax_error.GraphQLSyntaxError'])
def test_candidate_graphql_parser_failure_during_fixture_import(contract, name):
    raw = execution(one='harness_error')
    raw['checks'][0]['details'] = (
        'ERROR collecting tests/validation/test_validation.py\n'
        '/work/project/src/graphql/language/parser.py:289: in parse_definition\n'
        '    raise self.unexpected()\n'
        f"E   {name}: Syntax Error: Unexpected Name 'Mammal'.\n")
    score = score_product(contract, raw)
    assert score['status'] == 'failed' and score['gc'] == .5
    assert score['checks'][0]['original_status'] == 'harness_error'
    assert raw['checks'][0]['status'] == 'harness_error'



@pytest.mark.parametrize('tail', [
    'E   ModuleNotFoundError: missing_dependency\n',
    'E   RuntimeError: private fixture failed\n',
])
def test_candidate_error_does_not_mask_another_collection_failure(contract, tail):
    raw = execution(one='harness_error')
    raw['checks'][0]['details'] = (
        'ERROR collecting tests/test_parser.py\n'
        '/work/project/src/graphql/language/parser.py:289: in parse_definition\n'
        "E   graphql.error.syntax_error.GraphQLSyntaxError: Unexpected Name 'Mammal'\n" + tail)
    assert score_product(contract, raw)['status'] == 'verifier_error'



def test_graphql_exception_without_candidate_parser_evidence_stays_unscored(contract):
    raw = execution(one='harness_error')
    raw['checks'][0]['details'] = (
        'ERROR collecting tests/test_parser.py\n'
        '/work/grader/fixture.py:10: in setup\n'
        "E   graphql.error.syntax_error.GraphQLSyntaxError: broken fixture\n")
    assert score_product(contract, raw)['status'] == 'verifier_error'
