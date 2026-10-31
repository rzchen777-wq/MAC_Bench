"""Bounded standard SQL over explicitly supplied public data, never native databases."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import re
import sqlite3
import time

from .contracts import ToolError, encoded, object_schema
from .navigation import resolve

REFERENCE = object_schema({'result_ref': {'type': 'string', 'minLength': 1},
    'path': {'type': 'string', 'default': '/data'}}, ['result_ref'])
SOURCE = object_schema({
    'resource': {'type': 'string'}, 'result_ref': {'type': 'string'},
    'path': {'type': 'string'}, 'rows': {'type': 'array', 'maxItems': 50000},
    'decode_json': {'type': 'boolean'}}, [])
SOURCE['oneOf'] = [{'required': [key]} for key in ('resource', 'result_ref', 'rows')]
COMPUTE_SCHEMA = object_schema({
    'sources': {'type': 'object', 'minProperties': 0, 'maxProperties': 40,
        'propertyNames': {'pattern': '^[A-Za-z][A-Za-z0-9_]{0,39}$'}, 'additionalProperties': SOURCE},
    'sql': {'type': 'string', 'minLength': 1, 'maxLength': 40000},
    'parameters': {'type': 'array', 'maxItems': 1000,
        'items': {'type': ['string', 'number', 'integer', 'null', 'boolean']}},
    'output': {'enum': ['rows', 'json_value'], 'default': 'rows'},
    'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
}, ['sources', 'sql', 'request_id'])
DESCRIPTION = (
    'Run a bounded read-only SQLite SELECT/CTE over named input tables. Each source selects a public '
    'JSON resource, your saved result_ref, or literal rows; path is a JSON Pointer, decode_json parses '
    'an explicitly selected JSON string. Array objects become rows with their exact field names; '
    'nested objects/arrays are JSON strings usable with json_extract/json_each. Use JOIN, GROUP BY, '
    'DISTINCT, arithmetic, round and SQLite JSON functions. Pass values through parameters (SQL ?). '
    'output=rows returns data.rows; output=json_value decodes one JSON-text cell into '
    'data.value, useful for building tool arguments with json_object/json_group_array. Results are '
    'saved receipts: pass their reference to call_tool.arguments_ref or another compute source. '
    'No files, network, native database, task solution, grading or application writes are available. '
    'Public facts are initial evidence; saved results are observations, never automatically live state. '
    'Input/output limits: 50,000 rows and 10 MB each, at most 40 sources; complex queries stop on budget. '
    'Inspect full large results with read_result/query_result. No business choice is inferred.')

_FUNCTIONS = set(('abs coalesce ifnull nullif round min max sum avg total count length lower upper like glob '
    'trim ltrim rtrim substr substring replace instr printf format typeof unicode char hex quote '
    'iif likely unlikely likelihood json json_array json_object json_array_length json_extract '
    'json_type json_valid json_quote json_group_array json_group_object json_insert json_replace '
    'json_set json_remove json_patch json_error_position group_concat row_number rank dense_rank '
    'percent_rank cume_dist ntile lag lead first_value last_value nth_value date datetime julianday strftime').split())


def _calendar_functions(db):
    """SQLite date arithmetic with an explicit ISO input and no ambient clock/TZ."""
    calendar = sqlite3.connect(':memory:')
    calendar.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 10000)
    def call(name, *args):
        index = 1 if name == 'strftime' else 0
        if len(args) <= index or not isinstance(args[index], str):
            raise ValueError('Date operations require an explicit ISO date/time')
        value = args[index]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?', value):
            raise ValueError('Implicit clock inputs are unavailable')
        datetime.fromisoformat(value.replace('Z','+00:00'))
        for modifier in args[index+1:]:
            if not isinstance(modifier,str) or not (
                re.fullmatch(r'[+-]?\d+(?:\.\d+)? (?:days?|hours?|minutes?|seconds?|months?|years?)',modifier)
                or modifier in ('start of month','start of year','start of day')
                or re.fullmatch(r'weekday [0-6]',modifier)):
                raise ValueError('Use explicit date shifts, start-of-period or weekday modifiers')
        return calendar.execute('SELECT '+name+'('+','.join('?' for _ in args)+')',args).fetchone()[0]
    for name in ('date','datetime','julianday','strftime'):
        db.create_function(name,-1,lambda *args,_name=name:call(_name,*args),deterministic=True)
    return calendar


def result_value(agent, reference):
    """Read only this actor's immutable receipt; never accept arbitrary filesystem paths."""
    value, _ = agent.world.journal.receipt(agent.world.world_id, agent.actor,
        agent.role.name, reference['result_ref'])
    return deepcopy(resolve(value, reference.get('path', '/data')))


def _scalar(value):
    if isinstance(value, (dict, list)):
        return encoded(value)
    if isinstance(value, int) and not -(2**63) <= value < 2**63:
        raise ToolError('sql_integer_range', 'An input integer exceeds SQLite range; supply exact large identifiers as strings.')
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ToolError('invalid_data_source', 'Input cells must be JSON values.')
    return value


def execute_sql(sources, sql, parameters=(), output='rows'):
    """Each call gets a fresh in-memory DB. Authorizer forbids all writes/file access."""
    if output not in ('rows', 'json_value'):
        raise ToolError('invalid_arguments', 'output must be rows or json_value; omit it for ordinary table queries.')
    if len(encoded(sources).encode()) > 10_000_000:
        raise ToolError('data_input_too_large', 'Selected inputs exceed 10 MB; select smaller collections.')
    db = sqlite3.connect(':memory:')
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
    db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 512)
    db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 40000)
    db.execute('PRAGMA temp_store=MEMORY')
    db.execute('PRAGMA cache_size=-4096')
    calendar = _calendar_functions(db)
    if len({name.casefold() for name in sources}) != len(sources):
        calendar.close()
        db.close()
        raise ToolError('ambiguous_table_names', 'SQLite table aliases are case-insensitive; use distinct names.')
    counts, input_rows = {}, 0
    try:
        for name, rows in sources.items():
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,39}', name) or name.lower().startswith('sqlite_'):
                raise ToolError('invalid_table_name', 'Use a short SQL table alias, excluding sqlite_ names.')
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                raise ToolError('invalid_data_source', 'Select a JSON array or object; decode_json can parse a selected JSON string.')
            rows = [row if isinstance(row, dict) else {'value': row} for row in rows]
            input_rows += len(rows)
            if input_rows > 50000:
                raise ToolError('data_input_too_large', 'Selected inputs exceed 50,000 rows.')
            columns = list(dict.fromkeys(key for row in rows for key in row)) or ['value']
            if len(columns) > 256 or any(not isinstance(c, str) or not c or '\x00' in c for c in columns):
                raise ToolError('invalid_data_columns', 'Use at most 256 nonempty named columns per table.')
            if len({c.casefold() for c in columns}) != len(columns):
                raise ToolError('ambiguous_data_columns', 'SQLite column names are case-insensitive; rename conflicting fields explicitly.')
            quote = lambda s: '"' + s.replace('"', '""') + '"'
            db.execute('CREATE TABLE ' + quote(name) + ' (' + ','.join(map(quote, columns)) + ')')
            db.executemany('INSERT INTO ' + quote(name) + ' VALUES (' + ','.join('?' for _ in columns) + ')',
                [[_scalar(row.get(c)) for c in columns] for row in rows])
            counts[name] = {'rows': len(rows), 'columns': columns}
        allowed_reads = {name.casefold() for name in sources}
        def authorize(action, first, second, database, context):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and first.casefold() in allowed_reads:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and database is None and not first.lower().startswith('sqlite_'):
                # SQLite reports count(*) over an ephemeral CTE as a READ of
                # its CTE name with no database. It is not a filesystem table.
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() in _FUNCTIONS:
                return sqlite3.SQLITE_OK
            # json_each/json_tree are eponymous read-only virtual tables. Schema
            # initialization is forced before setting this authorizer below.
            if action == sqlite3.SQLITE_READ and first in ('json_each', 'json_tree'):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        db.execute("SELECT * FROM json_each('[]')").fetchall()
        db.execute("SELECT * FROM json_tree('[]')").fetchall()
        db.set_authorizer(authorize)
        deadline, ticks = time.monotonic() + 5, [0]
        def budget():
            ticks[0] += 1
            return ticks[0] > 20000 or time.monotonic() > deadline
        db.set_progress_handler(budget, 1000)
        cursor = db.execute(sql, parameters)
        if not cursor.description:
            raise ToolError('invalid_sql', 'Use a SELECT query that returns rows.')
        labels = [column[0] for column in cursor.description]
        if len(set(labels)) != len(labels):
            raise ToolError('duplicate_output_columns', 'Give selected columns distinct AS aliases.')
        rows, size = [], 0
        for row in cursor:
            item = dict(zip(labels, row))
            size += len(encoded(item).encode())
            if len(rows) >= 50000 or size > 10_000_000:
                raise ToolError('data_output_too_large', 'Query exceeds 50,000 output rows or 10 MB; narrow it or aggregate.')
            rows.append(item)
        result = {'input_tables': counts, 'row_count': len(rows), 'source': 'derived_data_not_application_state'}
        if output == 'json_value':
            if len(rows) != 1 or len(labels) != 1 or not isinstance(rows[0][labels[0]], str):
                raise ToolError('invalid_json_output',
                    f'Query returned {len(rows)} rows and {len(labels)} columns. '
                    'output=json_value requires exactly one cell containing JSON text. '
                    'For a table, omit output or set output="rows" and use a new request_id. '
                    'The query was read-only; no application state changed.')
            try:
                result['value'] = json.loads(rows[0][labels[0]])
            except ValueError:
                raise ToolError('invalid_json_output', 'The selected cell is not valid JSON. '
                    'For ordinary text or table results, omit output or set output="rows" and use a new request_id. '
                    'The query was read-only; no application state changed.') from None
        else:
            result['rows'] = rows
        return result
    except sqlite3.Error as exc:
        code = 'sql_budget_exceeded' if 'interrupted' in str(exc) else 'invalid_sql'
        raise ToolError(code, 'SQL failed: ' + str(exc)[:350] + '. Only bounded read-only SELECT queries and documented functions are supported.') from None
    finally:
        calendar.close()
        db.close()


def compute(agent, arguments):
    datasets, provenance = {}, {}
    for name, source in arguments['sources'].items():
        if 'resource' in source:
            resource = source['resource']
            if resource not in agent.world.data_resources:
                raise ToolError('unknown_resource', 'Use a public JSON resource listed in public/data_guide.md.')
            value = resolve(agent.world.data_resources[resource], source.get('path', ''))
            provenance[name] = {'resource': resource, 'path': source.get('path', '')}
        elif 'result_ref' in source:
            value = result_value(agent, source)
            provenance[name] = {'result_ref': source['result_ref'], 'path': source.get('path', '/data'),
                'source': 'saved_receipt_not_live_state'}
        else:
            value = source['rows']
            provenance[name] = {'source': 'agent_supplied_rows'}
        if source.get('decode_json'):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                raise ToolError('invalid_json_source', 'decode_json requires a selected valid JSON string.') from None
        provenance[name]['sha256'] = sha256(encoded(value).encode()).hexdigest()
        datasets[name] = value
    return {**execute_sql(datasets, arguments['sql'], arguments.get('parameters', ()), arguments.get('output', 'rows')),
        'provenance': provenance}
