"""Public task tools for scripts in the shared workspace (standard library only).

    from mac_tools import call, load_result
    resources = call('list_resources')
    receipt = call('call_tool', tool_name='...', arguments={...}, request_id='...')
    # Check receipt['status'] before using the result or deciding to retry.
    observation = load_result(receipt['result_ref'])
    saved_data = observation['data']

Calls use the launching agent's identity, permissions and episode budget.
Responses have exactly the same schema as direct tool responses. No automatic
retry or application pagination is performed. load_result reads one saved
observation, not current application state. Save data with json.dump / CSV and
use a new application query when current state is needed.
"""
import json
import os
from pathlib import Path
import socket
import sys


class ToolClientError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def definitions():
    """Return the public tool descriptions and input schemas installed for this task."""
    return json.loads(Path(__file__).with_name('tools.json').read_text())


def call(name, **arguments):
    """Call a public tool once. Business rejections remain ordinary tool responses."""
    address = os.environ.get('MAC_BENCH_TOOLS_SOCKET')
    if not address:
        raise ToolClientError('TOOLS_UNAVAILABLE', 'Public tool access is not enabled for this process.')
    if not isinstance(name, str) or not name:
        raise ValueError('Tool name must be a nonempty string.')
    request = json.dumps({'name': name, 'arguments': arguments}, allow_nan=False).encode() + b'\n'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(600)
            try:
                connection.connect(address)
            except OSError as exc:
                raise ToolClientError('TOOL_CONNECTION',
                    'Could not connect to public tools; the request was not sent. ' + str(exc)) from exc
            connection.sendall(request)
            with connection.makefile('rb') as response:
                line = response.readline()
        if not line:
            raise ConnectionError('Tool connection closed before returning a result')
        payload = json.loads(line)
    except (OSError, ValueError, ConnectionError) as exc:
        raise ToolClientError('TOOL_CONNECTION',
            'No tool result was received. The operation may have run; inspect its request ID '
            'and current state before retrying. ' + str(exc)) from exc
    if 'error' in payload:
        raise ToolClientError(payload['error']['code'], payload['error']['message'])
    return payload['result']


def load_result(result_ref, path='/data'):
    """Load a complete observation, including its source, time, path and data.

    Follows navigation pages and nested value paths without dropping fields.
    This does not follow native application pagination or issue a fresh query.
    Each read counts as a tool call. An error raises instead of returning a
    partially assembled value.
    """
    metadata = {}
    def read(path):
        cursor, seen = None, set()
        value, expected, kind = None, None, None
        while True:
            arguments = {'result_ref': result_ref, 'path': path, 'limit': 100}
            if cursor:
                arguments['cursor'] = cursor
            page = call('read_result', **arguments)
            if 'error' in page:
                error = page['error']
                raise ToolClientError(error.get('code', 'RESULT_READ'), error.get('message', str(error)))
            if not metadata:
                metadata.update({key: page[key] for key in ('result_ref', 'observed_at', 'source', 'path')})
            if kind is None:
                kind = page['kind']
                expected = page.get('total', page.get('characters'))
                value = {} if kind == 'object' else [] if kind == 'array' else '' if kind == 'string' else page['value']
            elif kind != page['kind'] or expected != page.get('total', page.get('characters')):
                raise ToolClientError('RESULT_READ', 'Saved result changed during navigation.')
            if kind in ('object', 'array'):
                for entry in page['items']:
                    item = entry['value'] if entry['complete'] else read(entry['path'])
                    if kind == 'array':
                        if entry['key'] != len(value):
                            raise ToolClientError('RESULT_READ', 'Saved result has a missing or repeated array item.')
                        value.append(item)
                    else:
                        if entry['key'] in value:
                            raise ToolClientError('RESULT_READ', 'Saved result has a repeated object field.')
                        value[entry['key']] = item
            elif kind == 'string':
                if page['offset'] != len(value):
                    raise ToolClientError('RESULT_READ', 'Saved result has a missing or repeated text segment.')
                value += page['value']
            cursor = page.get('next_cursor')
            if not cursor:
                if expected is not None and len(value) != expected:
                    raise ToolClientError('RESULT_READ', 'Saved result is incomplete.')
                return value
            if cursor in seen:
                raise ToolClientError('RESULT_READ', 'Saved result returned a repeated continuation cursor.')
            seen.add(cursor)
    data = read(path)
    return {**metadata, 'data': data}


def main():
    if len(sys.argv) == 2 and sys.argv[1] == '--list':
        result = definitions()
    elif 2 <= len(sys.argv) <= 3:
        result = call(sys.argv[1], **(json.loads(sys.argv[2]) if len(sys.argv) == 3 else {}))
    else:
        sys.exit('Usage: mac-tools --list | mac-tools TOOL_NAME [JSON_ARGUMENTS]')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
