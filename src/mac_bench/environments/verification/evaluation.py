"""Fixed verdict semantics over SQL measurements and violation rows."""
from mac_bench.environments.verification.contracts import verify_package
from mac_bench.environments.verification.errors import VerificationError
from mac_bench.environments.verification.sql import QueryEngine
from mac_bench.environments.verification.scoring import score_goals


def evaluate(contract, current, *, provenance='supplied_snapshot', max_steps=20_000_000):
    verify_package(contract)
    results = []
    try:
        with QueryEngine(contract['initial'], current, contract['identities'], contract['layout'],
                         facts=contract['facts'], links=contract['links'], max_steps=max_steps) as engine:
            for check in contract['checks']:
                record = {k: check[k] for k in ('id', 'clause', 'description')}
                if 'category' in check:
                    record['category'] = check['category']
                try:
                    outcome = engine.check(check['sql'], check['binding']['collections'], assertion=check['assertion'])
                    record.update(status='passed' if outcome['passed'] else 'failed',
                                  violations=outcome['violations'], evidence_truncated=outcome['evidence_truncated'])
                except (VerificationError, ValueError, TypeError) as exc:
                    record.update(status='error', error=str(exc))
                results.append(record)
    except (VerificationError, ValueError, TypeError, KeyError) as exc:
        return {'status': 'evaluation_error', 'complete': False, 'error': str(exc), 'checks': results,
                **score_goals(contract, results, 'evaluation_error'),
                'verifier_revision': contract['revision'], 'provenance': provenance,
                'native_execution_verified': False}
    unresolved = contract['gaps'] or contract['requirements']['ambiguities']
    status = ('unsupported' if unresolved else 'evaluation_error' if any(r['status'] == 'error' for r in results)
              else 'passed' if results and all(r['status'] == 'passed' for r in results) else 'failed')
    diagnostics = {}
    for result in results:
        if result['status'] != 'passed' and 'category' in result:
            diagnostics.setdefault(result['category'], []).append(result['id'])
    return {'status': status, 'complete': status == 'passed', 'checks': results,
            **({'diagnostics': diagnostics} if any('category' in r for r in results) else {}),
            **score_goals(contract, results, status),
            'gaps': contract['gaps'], 'ambiguities': contract['requirements']['ambiguities'],
            'verifier_revision': contract['revision'], 'provenance': provenance,
            'native_execution_verified': provenance == 'trusted_native_readback'}
