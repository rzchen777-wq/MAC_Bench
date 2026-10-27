"""Pure adapter payload expressions; no code evaluation or network access."""
from copy import deepcopy
from decimal import Decimal

from mac_bench.environments.errors import CompileFailure


def payload_pointers(template):
    """Validate template syntax and expose reads without evaluating values."""
    if isinstance(template, list):
        return set().union(*(payload_pointers(value) for value in template))
    if not isinstance(template, dict):
        return set()
    if set(template) == {'$literal'}:
        return set()
    if set(template) == {'$pointer'}:
        pointer = template['$pointer']
        if not isinstance(pointer, str) or pointer and not pointer.startswith('/'):
            raise CompileFailure('payload_pointer_invalid')
        return {pointer}
    if set(template) == {'$subtract'}:
        operands = template['$subtract']
        if not isinstance(operands, list) or len(operands) != 2:
            raise CompileFailure('lifecycle_payload_subtraction_arity')
        return payload_pointers(operands)
    if set(template) == {'$decimal'}:
        spec = template['$decimal']
        if (not isinstance(spec, dict) or set(spec) != {'minor_units', 'scale'}
                or type(spec['scale']) is not int or not 0 <= spec['scale'] <= 6):
            raise CompileFailure('lifecycle_payload_decimal_contract')
        return payload_pointers(spec['minor_units'])
    if any(not isinstance(name, str) or name.startswith('$') for name in template):
        raise CompileFailure('unknown_lifecycle_payload_expression')
    return set().union(*(payload_pointers(value) for value in template.values()))


def validate_identity_expression(expression, *, roots=('result', 'arguments', 'after')):
    """An identity must be read from the response or its native call context.

    Composite locators assemble independently sourced components. Constants,
    arithmetic and empty objects cannot manufacture an operation's identity.
    """
    if not isinstance(expression, dict) or not expression:
        raise CompileFailure('produced_identity_requires_observation_expression')
    if set(expression) == {'$pointer'}:
        pointer = expression['$pointer']
        if (not isinstance(pointer, str) or not pointer.startswith(tuple('/' + root + '/' for root in roots))
                or any(token in {'', '-'} for token in pointer.split('/')[1:])):
            raise CompileFailure('produced_identity_pointer_invalid')
        return
    for name, child in expression.items():
        if not isinstance(name, str) or not name or name.startswith('$'):
            raise CompileFailure('produced_identity_component_invalid')
        validate_identity_expression(child, roots=roots)


def _value(document, pointer):
    value = document
    if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
        raise CompileFailure("payload_pointer_invalid")
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, TypeError, ValueError):
            raise CompileFailure("payload_pointer_unresolved") from None
    return value


def payload_value(template, arguments):
    if not isinstance(template, (dict, list)):
        return template
    if isinstance(template, list):
        return [payload_value(value, arguments) for value in template]
    if set(template) == {'$pointer'}:
        return deepcopy(_value(arguments, template['$pointer']))
    if set(template) == {'$literal'}:
        return deepcopy(template['$literal'])
    if set(template) == {'$subtract'}:
        operands = template['$subtract']
        if not isinstance(operands, list) or len(operands) != 2:
            raise CompileFailure('lifecycle_payload_subtraction_arity')
        left, right = [payload_value(value, arguments) for value in operands]
        if type(left) is not int or type(right) is not int:
            raise CompileFailure('lifecycle_payload_subtraction_requires_integers')
        return left - right
    if set(template) == {'$decimal'}:
        spec = template['$decimal']
        if set(spec) != {'minor_units', 'scale'} or type(spec['scale']) is not int or not 0 <= spec['scale'] <= 6:
            raise CompileFailure('lifecycle_payload_decimal_contract')
        value = payload_value(spec['minor_units'], arguments)
        if type(value) is not int:
            raise CompileFailure('lifecycle_payload_decimal_requires_minor_units')
        return format(Decimal(value).scaleb(-spec['scale']), f'.{spec["scale"]}f')
    if any(name.startswith('$') for name in template):
        raise CompileFailure('unknown_lifecycle_payload_expression')
    return {name: payload_value(value, arguments) for name, value in template.items()}
