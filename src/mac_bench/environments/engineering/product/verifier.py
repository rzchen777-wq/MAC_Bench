"""Aggregate trusted checks over frozen product goals; harness errors are distinct."""
import re


def candidate_collection_failure(check):
    """Recognize a pytest collection failure inside submitted Python code.

    Native tests may call the candidate while importing their fixtures. A
    programming exception there fails that capability. Missing dependencies,
    test configuration, interruptions and transport errors remain unscored.
    Preserve the raw observation and mark the classification explicitly.
    """
    details = check.get('details', '')
    if check.get('status') != 'harness_error' or not isinstance(details, str):
        return check
    collecting = re.search(r'ERROR collecting |(?m:^ERROR tests/)', details)
    candidate_frame = re.search(r'(?m)^/work/project/[^\n:]+\.py:\d+: in |^E\s+File "/work/project/[^"\n]+\.py", line \d+', details)
    # Pytest prints application exceptions with their qualified class name.
    # Inspect every reported exception: a candidate error must not hide a
    # missing dependency or an unrelated harness error in the same collection.
    exceptions = re.findall(r'(?m)^E\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):', details)
    programming_errors = {'AttributeError', 'TypeError', 'SyntaxError',
                          'IndentationError', 'NameError', 'UnboundLocalError',
                          'NotImplementedError'}
    graphql_parser = re.search(r'(?m)^/work/project/[^\n:]*graphql/language/parser\.py:\d+: in ', details)
    def candidate_exception(name):
        return name in programming_errors or (bool(graphql_parser) and name in {
            'GraphQLSyntaxError', 'graphql.error.syntax_error.GraphQLSyntaxError'})
    if collecting and candidate_frame and exceptions and all(map(candidate_exception, exceptions)):
        return {**check, 'status': 'failed', 'original_status': 'harness_error',
                'classification': 'candidate_python_exception_during_test_collection'}
    return check

def score_product(contract, execution):
    declared = {c["id"] for c in contract["checks"]}
    observations = [candidate_collection_failure(check) for check in execution.get("checks", [])]
    by_id = {c["id"]: c for c in observations}
    if len(by_id) != len(observations) or set(by_id) != declared:
        return {"status": "verifier_error", "task_success": None, "gc": None,
                "error": "Verifier returned missing, duplicate or undeclared check IDs"}
    if any(c.get("status") not in {"passed", "failed", "blocked", "harness_error"}
           for c in observations):
        return {"status": "verifier_error", "task_success": None, "gc": None,
                "error": "Unknown check status"}
    if execution.get("status") == "harness_error" or any(
            c["status"] == "harness_error" for c in observations):
        return {"status": "verifier_error", "task_success": None, "gc": None,
                "checks": observations, "error": execution.get("error")}
    requirements = {r["id"]: all(by_id[c]["status"] == "passed" for c in r["check_ids"])
                    for r in contract["requirements"]}
    goals = [{"id": g["id"], "passed": all(requirements[r] for r in g["requirement_ids"])}
             for g in contract["goals"]]
    regression = [c["id"] for c in contract["checks"] if c.get("role") == "regression"]
    regression_ok = all(by_id[c]["status"] == "passed" for c in regression)
    success = all(g["passed"] for g in goals) and regression_ok
    return {"status": "passed" if success else "failed", "task_success": success,
            "gc": sum(g["passed"] for g in goals) / len(goals), "goals": goals,
            "requirements": requirements, "regression_passed": regression_ok,
            "checks": observations}


def construction_gate(contract, reference, starter, negative_cases):
    errors = []
    if reference.get("task_success") is not True:
        errors.append("Reference must pass the complete frozen verifier")
    if starter.get("status") == "verifier_error":
        errors.append("Starter has a verifier/harness error")
    if starter.get("regression_passed") is not True:
        errors.append("Declared retained behavior must work in the starter")
    if any(g["passed"] for g in starter.get("goals", [])):
        errors.append("An assigned goal is already complete in the starter")
    if len(starter.get("goals", [])) != len(contract["goals"]):
        errors.append("Starter goal observations are incomplete")
    if not negative_cases:
        errors.append("Meaningful negative cases are required")
    for case in negative_cases:
        score = case["score"]
        if score.get("status") == "verifier_error":
            errors.append(f"Negative case {case['id']} caused a harness error")
        observed = {c["id"]: c["status"] for c in score.get("checks", [])}
        if not case["must_fail"] or not all(observed.get(c) == "failed" for c in case["must_fail"]):
            errors.append(f"Negative case {case['id']} escaped its required behavior checks")
    return {"status": "ready" if not errors else "needs_curation", "errors": errors}
