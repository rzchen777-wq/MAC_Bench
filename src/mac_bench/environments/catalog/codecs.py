"""Explicit exact scalar conversions at the native/catalog boundary."""
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from jsonschema import Draft202012Validator

from mac_bench.environments.errors import CompileFailure


def convert_scalar(field, value, *, to_native):
    """Exact declared decimal-string conversion; never use binary floats."""
    scale = field['codec']['scale']
    if value is None:
        result = None
    elif to_native:
        if type(value) is not int:
            raise CompileFailure('money_codec_requires_integer_minor_units')
        digits = str(abs(value)).zfill(scale + 1)
        result = ('-' if value < 0 else '') + (digits[:-scale] + '.' + digits[-scale:] if scale else digits)
    else:
        if (not isinstance(value, str) or re.fullmatch(
                r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', value) is None):
            raise CompileFailure('money_codec_requires_native_decimal_string')
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise CompileFailure('money_codec_invalid_decimal_string') from None
        if not amount.is_finite():
            raise CompileFailure('money_codec_nonfinite_amount')
        sign, digits, exponent = amount.as_tuple()
        coefficient = int(''.join(map(str, digits)))
        shift = exponent + scale
        if shift >= 0:
            result = coefficient * 10**shift
        else:
            result, remainder = divmod(coefficient, 10**(-shift))
            if remainder:
                raise CompileFailure('money_codec_requires_rounding')
        if sign:
            result = -result
    native = result if to_native else value
    validator = Draft202012Validator(field['schema'])
    if to_native and value is not None and next(validator.iter_errors(native), None):
        # Enumerated wire spellings may differ from fixed-scale formatting.
        # Choose only an explicitly allowed spelling with exactly equal value.
        for candidate in field['schema'].get('enum', ()):
            if candidate is not None and convert_scalar(field, candidate, to_native=False) == value:
                return candidate
    if next(validator.iter_errors(native), None):
        raise CompileFailure('money_codec_value_violates_native_schema')
    return result



def map_scalar_codecs(field, value, *, to_native):
    if 'codec' in field:
        return convert_scalar(field, value, to_native=to_native)
    if value is None:
        return None
    if field['kind'] in {'array', 'set'} and 'items' in field:
        return [map_scalar_codecs(field['items'], item, to_native=to_native) for item in value]
    if field['kind'] == 'object':
        result = {}
        for name, item in value.items():
            child = field['properties'].get(name, field.get('additional_properties'))
            result[name] = (map_scalar_codecs(child, item, to_native=to_native)
                            if isinstance(child, dict) else deepcopy(item))
        return result
    return deepcopy(value)

