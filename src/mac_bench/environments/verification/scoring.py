"""Frozen business goals, never SQL statement counts, define partial credit."""


def score_goals(contract, checks, status):
    by_clause = {}
    for check in checks:
        by_clause.setdefault(check['clause'], []).append(check['status'])
    gaps = {gap['clause'] for gap in contract['gaps']}
    clauses = []
    for clause in contract['requirements']['clauses']:
        cid = clause['id']
        states = by_clause.get(cid, [])
        verdict = (
            'unsupported' if cid in gaps else
            'error' if not states or 'error' in states else
            'passed' if all(s == 'passed' for s in states) else 'failed'
        )
        clauses.append({'id': cid, 'status': verdict})
    verdicts = {c['id']: c['status'] for c in clauses}
    goals = []
    for goal in contract['requirements']['goals']:
        states = [verdicts[c] for c in goal['clause_ids']]
        verdict = (
            'unsupported' if 'unsupported' in states else
            'error' if 'error' in states else
            'passed' if all(s == 'passed' for s in states) else 'failed'
        )
        goals.append({**goal, 'status': verdict})
    # Never shrink the denominator or count infrastructure failures as task failures.
    available = status in ('passed', 'failed')
    return {
        'task_success': status == 'passed' if available else None,
        'gc': sum(g['status'] == 'passed' for g in goals) / len(goals) if available else None,
        'goals': goals,
        'clauses': clauses,
        'global_clause_ids': contract['requirements']['global_clause_ids'],
    }
