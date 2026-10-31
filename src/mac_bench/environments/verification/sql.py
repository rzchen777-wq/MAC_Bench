"""Read-only SQLite checks over frozen projections; no model code execution."""
import math
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from mac_bench.environments.verification.errors import VerificationError
from mac_bench.environments.verification.snapshot import (
    SYSTEM_COLUMNS,
    identity_parameters,
    project,
    quote,
    sql_value,
)

FUNCTIONS = frozenset('abs avg coalesce count glob hex ifnull instr length like lower ltrim max min nullif replace round rtrim substr substring sum total trim typeof upper unicode json_array json_array_length json_extract json_object json_quote json_type json_valid group_concat money_minor iso_date utc_timestamp elapsed_seconds try_utc_timestamp try_elapsed_seconds json_object_document integer_id'.split())
FUNCTIONS = FUNCTIONS | frozenset(('json_remove', 'checked_money_minor', 'checked_iso_date',
                                  'checked_utc_timestamp', 'checked_elapsed_seconds', 'normalized_text'))


def normalized_text(value):
    """Normalize presentation in explicitly selected descriptive text fields."""
    return ' '.join(value.split()).casefold() if isinstance(value, str) else None


def install_checked_value_functions(connection):
    """Reject malformed values without letting SQL NULL make a check pass.

    Strict historical UDFs are unchanged. Only explicitly opted-in checked_*
    calls record invalid inputs. Database/schema/budget failures remain errors.
    """
    invalid = set()
    def checked(name, operation):
        def call(*arguments):
            try:
                if operation is money_minor and Decimal(str(arguments[0])).adjusted() > 18:
                    invalid.add(name)
                    return None
                value = operation(*arguments)
                if type(value) is int and not -(2**63) <= value < 2**63:
                    invalid.add(name)
                    return None
                if value is None:
                    invalid.add(name)
                return value
            except (ValueError, TypeError, ArithmeticError, AttributeError):
                invalid.add(name)
                return None
        return call
    for name, operation, arities in (
        ('money_minor', money_minor, (1, 2)), ('iso_date', iso_date, (1,)),
        ('utc_timestamp', utc_timestamp, (1,)), ('elapsed_seconds', elapsed_seconds, (2,))):
        for arity in arities:
            connection.create_function('checked_' + name, arity, checked(name, operation), deterministic=True)
    connection.create_function('normalized_text', 1, normalized_text, deterministic=True)
    return invalid


def json_object_document(value):
    """Read exactly one JSON object, raw or in a single Markdown code fence.

    No prose extraction, first-object guessing, duplicate keys or non-JSON numeric
    constants. Return NULL for malformed/ambiguous documents so graders can
    report a record-format failure instead of a SQL execution error.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', text, re.IGNORECASE)
        if match is None:
            return None
        text = match.group(1)
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = item
        return result
    def invalid_constant(value):
        raise ValueError('Invalid JSON constant')
    try:
        document = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(document, dict):
            return None
        return json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


def json_keys_match(value, keys):
    """Compare actual object keys, rejecting duplicate keys and malformed JSON."""
    document = json_object_document(value)
    try:
        expected = json.loads(keys)
        return int(document is not None and isinstance(expected, list)
                   and all(isinstance(key, str) for key in expected)
                   and set(json.loads(document)) == set(expected))
    except (ValueError, TypeError, RecursionError):
        return 0


FUNCTIONS = FUNCTIONS | {'json_keys_match'}


def integer_id(value, json_type):
    """Normalize only explicitly selected integer IDs, preserving type checks.

    Callers supply json_type alongside json_extract: SQLite otherwise turns a
    JSON boolean into 0/1. Floats, booleans, signs, exponents, leading zeroes and
    whitespace are not accepted as canonical native IDs. No amounts, order
    codes, UUIDs or other fields are implicitly converted.
    """
    if json_type == 'integer' and type(value) is int and 0 <= value <= 2**63 - 1:
        return value
    if json_type == 'text' and isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value):
        if len(value) > 19:
            return None
        result = int(value)
        return result if result <= 2**63 - 1 else None
    return None


def money_minor(value, scale=2):
    if value is None or isinstance(value, bool) or type(scale) is not int or not 0 <= scale <= 6:
        raise ValueError('Invalid monetary value or scale')
    try:
        amount = Decimal(str(value)) * (Decimal(10) ** scale)
    except InvalidOperation as exc:
        raise ValueError('Invalid monetary number') from exc
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError('Money has fractional minor units')
    return int(amount)


def iso_date(value):
    """Return the written local calendar date; no implicit timezone conversion."""
    return datetime.fromisoformat(value.replace('Z', '+00:00')).date().isoformat()


def utc_timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp requires explicit timezone')
    return parsed.astimezone(timezone.utc).isoformat()


def try_utc_timestamp(value):
    """Normalize a supplied zoned time; invalid business input becomes SQL NULL.

    Callers must explicitly reject NULL for required fields. The strict function
    remains available for trusted facts; engine/schema errors are not caught.
    """
    if not isinstance(value, str):
        return None
    try:
        return utc_timestamp(value)
    except (ValueError, OverflowError):
        return None


def try_elapsed_seconds(start, end):
    """Return a duration only when both supplied times can be normalized."""
    start, end = try_utc_timestamp(start), try_utc_timestamp(end)
    if start is None or end is None:
        return None
    return elapsed_seconds(start, end)


def elapsed_seconds(start, end):
    """Duration between explicit zoned timestamps; no implicit wall clock."""
    if start is None or end is None:
        return None
    values = []
    for value in (start, end):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Timestamp requires explicit timezone')
        values.append(parsed.astimezone(timezone.utc))
    return (values[1] - values[0]).total_seconds()


class QueryEngine:
    def __init__(self, initial, current, identities, layout, *, facts=None, links=None,
                 max_steps=2_000_000, max_rows=100000):
        self.connection = sqlite3.connect(':memory:', cached_statements=0)
        self.remaining = max_steps
        self.layout = layout
        self.reads = set()
        self.allowed = set()
        try:
            self.parameters = identity_parameters(identities)
            db = self.connection
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2_000_000)
            db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100000)
            for stage, snapshot in [('initial', initial), ('current', current)]:
                db.execute(f'ATTACH DATABASE ":memory:" AS {stage}')
                rows = project(snapshot, layout, max_rows=max_rows)
                for table in layout:
                    columns = SYSTEM_COLUMNS + [c['name'] for c in table['columns']]
                    qualified = stage + '.' + quote(table['name'])
                    db.execute(f'CREATE TABLE {qualified} ({",".join(quote(c) for c in columns)})')
                    db.executemany(f'INSERT INTO {qualified} VALUES ({",".join("?" for _ in columns)})', rows[table['name']])
            db.execute('CREATE TABLE refs(object_key TEXT, entity TEXT, identity_field TEXT, identity_value)')
            for key, obj in identities.items():
                for field, value in obj['identity'].items():
                    db.execute('INSERT INTO refs VALUES(?,?,?,?)', (key, obj.get('entity'), field, sql_value(value)))
            db.execute('CREATE TABLE facts(name TEXT PRIMARY KEY, value)')
            db.executemany('INSERT INTO facts VALUES(?,?)', [(k, sql_value(v)) for k, v in (facts or {}).items()])
            db.execute('CREATE TABLE links(group_key TEXT, object_key TEXT, UNIQUE(group_key,object_key))')
            for row in links or []:
                if set(row) != {'group_key', 'object_key'} or row['object_key'] not in identities:
                    raise VerificationError('Unknown construction identity link')
                db.execute('INSERT INTO links VALUES(?,?)', (row['group_key'], row['object_key']))
            db.execute('CREATE TABLE linked_identities AS SELECT l.group_key, r.* FROM links l JOIN refs r ON r.object_key=l.object_key')
            db.commit()
            db.create_function('money_minor', 1, money_minor, deterministic=True)
            db.create_function('money_minor', 2, money_minor, deterministic=True)
            db.create_function('iso_date', 1, iso_date, deterministic=True)
            db.create_function('utc_timestamp', 1, utc_timestamp, deterministic=True)
            db.create_function('elapsed_seconds', 2, elapsed_seconds, deterministic=True)
            db.create_function('try_utc_timestamp', 1, try_utc_timestamp, deterministic=True)
            db.create_function('try_elapsed_seconds', 2, try_elapsed_seconds, deterministic=True)
            db.create_function('json_object_document', 1, json_object_document, deterministic=True)
            db.create_function('json_keys_match', 2, json_keys_match, deterministic=True)
            db.create_function('integer_id', 2, integer_id, deterministic=True)
            self.invalid_value_functions = install_checked_value_functions(db)
            db.execute('PRAGMA query_only=ON')
            db.set_authorizer(self._authorize)
            db.set_progress_handler(self._progress, 100)
        except Exception as exc:
            self.connection.close()
            if isinstance(exc, VerificationError):
                raise
            raise VerificationError("Cannot construct SQL snapshot: " + str(exc)) from exc

    def _progress(self):
        self.remaining -= 100
        return int(self.remaining < 0)

    def _authorize(self, action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_RECURSIVE:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if (arg2 or '').lower() in FUNCTIONS else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            if (database == 'main' and arg1 in ('refs', 'facts', 'links', 'linked_identities')) or (database in ('initial', 'current') and arg1 in self.allowed):
                self.reads.add((database, arg1, arg2))
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _prepare(self, sql, collections):
        if not isinstance(sql, str) or not sql.strip():
            raise VerificationError('Missing SQL query')
        self.allowed = {t['name'] for t in self.layout if t['collection'] in collections}
        self.reads.clear()
        self.connection.execute('EXPLAIN ' + sql, self.parameters).fetchall()
        if not any(stage == 'current' for stage, _, _ in self.reads):
            raise VerificationError('A check must read a current-state table')

    def check(self, sql, collections, *, evidence_limit=20, assertion=None):
        assertion = {'mode': 'no_rows'} if assertion is None else assertion
        from mac_bench.environments.verification.contracts import (
            ASSERTION,
            schema_check,
        )
        schema_check(assertion, ASSERTION)
        try:
            self.invalid_value_functions.clear()
            self._prepare(sql, collections)
            cursor = self.connection.execute(sql, self.parameters)
            columns = [c[0] for c in cursor.description or []]
            if not columns or len(columns) != len(set(columns)):
                raise VerificationError('SQL evidence requires uniquely named columns')
            rows = cursor.fetchmany(evidence_limit + 1)
            if self.invalid_value_functions:
                return {'passed': False, 'violations': [{'reason': 'invalid_business_value',
                        'functions': sorted(self.invalid_value_functions)}], 'evidence_truncated': False}
            if assertion['mode'] == 'scalar':
                if len(columns) != 1 or len(rows) != 1:
                    raise VerificationError('Scalar check must return exactly one row and one column')
                actual, expected = rows[0][0], assertion['expected']
                if actual is not None and (not isinstance(actual, (int, float)) or not math.isfinite(actual)):
                    raise VerificationError('Scalar check must return a finite number or NULL')
                passed = actual is not None and {'eq': actual == expected,
                    'le': actual <= expected if actual is not None else False,
                    'ge': actual >= expected if actual is not None else False}[assertion['operator']]
                return {'passed': passed, 'violations': [] if passed else [
                    {'actual': actual, 'operator': assertion['operator'], 'expected': expected}],
                    'evidence_truncated': False}
            return {'passed': not rows, 'violations': [dict(zip(columns, row)) for row in rows[:evidence_limit]],
                    'evidence_truncated': len(rows) > evidence_limit}
        except sqlite3.Error as exc:
            raise VerificationError('SQL check failed: ' + str(exc)) from exc

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
