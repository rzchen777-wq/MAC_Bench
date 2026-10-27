"""Function calling, authenticated HTTP and MCP share the same agent session."""
from copy import deepcopy
from mac_bench.construction.realize.tools.query import QUERY_PROPERTIES, QUERY_DESCRIPTION

from mac_bench.construction.realize.tools.contracts import (
    ToolError,
    object_schema,
    validate,
)

_TEXT = {'type': 'string', 'minLength': 1}
INTERFACE_VERSION = 2
SCHEMAS = {
    'list_apps': object_schema(),
    'list_tools': object_schema({'app': _TEXT, 'query': {'type': 'string'}, 'cursor': _TEXT,
                                  'entity': _TEXT, 'read_only': {'type': 'boolean'},
                                  'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}),
    'describe_tool': object_schema({'tool_name': _TEXT,
        'section': {'enum': ['usage', 'input', 'fields', 'output', 'lifecycle', 'errors', 'examples']},
        'path': {'type': 'string'}, 'cursor': _TEXT,
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ['tool_name']),
    'call_tool': object_schema({'tool_name': _TEXT, 'arguments': {'type': 'object'},
                                'request_id': {**_TEXT, 'maxLength': 128}}, ['tool_name', 'arguments', 'request_id']),
    'read_result': object_schema({'result_ref': _TEXT, 'path': {'type': 'string'}, 'cursor': _TEXT,
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ['result_ref']),
    'query_result': object_schema({'result_ref': _TEXT, **QUERY_PROPERTIES}, ['result_ref']),
}
DESCRIPTIONS = {
    'list_apps': 'List applications accessible to your fixed world and role.',
    'list_tools': 'Find native business operations. query matches all words in tool names/descriptions, case-insensitively; camelCase and underscores are treated as word boundaries. Use an entity name such as task or shift assignment; no semantic/synonym search. Page with next_cursor; omit query to list all operations. Copy returned tool names exactly, including suffixes.',
    'describe_tool': 'Read an operation usage card (default), or navigate exact input, field, output, lifecycle, error and example documents. path is a returned JSON Pointer; follow schema $ref in section=input. Nested details are linked, not discarded. Use next_cursor to continue the same path/limit.',
    'call_tool': 'Execute one registered operation. Put the operation input directly in arguments, following its usage/input documentation. Twenty filter/id/data and optional fields are siblings, without another arguments wrapper. Use a new request_id for each different call or fresh read; reuse it only to recover the same receipt. accepted/unknown do not establish completion. Large results supply result_ref for read_result.',
    'read_result': 'Read your saved tool result without re-executing it. result_ref comes from call_tool; path is a JSON Pointer (default /data). Follow returned paths and next_cursor to read exact nested values or long strings. Saved data is historical: use a fresh native read to check current state.',
    'query_result': 'Query a saved call_tool receipt, not live state; no app operation is repeated. '
        'Default path=/data. Native pagination is separate: aggregates cover only this saved receipt. ' + QUERY_DESCRIPTION,
}


def function_tools():
    return [{'type': 'function', 'function': {'name': name, 'description': DESCRIPTIONS[name],
                                             'parameters': deepcopy(schema)}} for name, schema in SCHEMAS.items()]


def dispatch(agent, name, arguments):
    if name not in SCHEMAS:
        raise ToolError('unknown_entrypoint', 'Unknown tool API entrypoint.')
    validate(SCHEMAS[name], arguments)
    return getattr(agent, name)(**arguments)


def create_http_app(sessions):
    """Token -> pre-bound AgentTools. No caller-controlled world, actor or role.

    The deployment supplies bearer tokens over its authenticated transport.
    Only session tokens belong here; native app credentials stay in connections.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool
    if not sessions or any(not isinstance(k, str) or not k for k in sessions):
        raise ValueError('Explicit session tokens are required')
    app = FastAPI(title='Collaboration World Tools', docs_url=None, redoc_url=None, openapi_url=None)
    sessions = dict(sessions)

    @app.post('/tools/{entrypoint}')
    async def invoke(entrypoint: str, request: Request):
        authorization = request.headers.get('authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else None
        agent = sessions.get(token)
        if agent is None:
            return JSONResponse({'error': {'code': 'unauthorized'}}, status_code=401)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 1_000_000:
                return JSONResponse({'error': {'code': 'request_too_large'}}, status_code=413)
        try:
            import json
            arguments = json.loads(data)
            result = await run_in_threadpool(dispatch, agent, entrypoint, arguments)
            return JSONResponse(result)
        except ToolError as exc:
            return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=400)
        except (ValueError, UnicodeError):
            return JSONResponse({'error': {'code': 'invalid_json'}}, status_code=400)
    return app


def create_mcp_server(agent):
    """One pre-bound actor endpoint; use stdio or an authenticated ASGI mount.

    SDK 1.x is explicitly pinned by the tool-api extra. No independent execution
    logic or business-function schemas are duplicated in this transport adapter.
    """
    from mcp.server.fastmcp import FastMCP
    server = FastMCP('Collaboration World Tools', json_response=True,
                     instructions='Discover applications and operations, read exact contracts, then execute native business actions. No grader or reset tools are exposed.')

    @server.tool(description=DESCRIPTIONS['list_apps'])
    def list_apps() -> dict:
        return agent.list_apps()

    @server.tool(description=DESCRIPTIONS['list_tools'])
    def list_tools(app: str | None = None, query: str | None = None,
                   cursor: str | None = None, limit: int = 20,
                   entity: str | None = None, read_only: bool | None = None) -> dict:
        return agent.list_tools(app, query, cursor, limit, entity, read_only)

    @server.tool(description=DESCRIPTIONS['describe_tool'])
    def describe_tool(tool_name: str, section: str = 'usage', path: str = '',
                      cursor: str | None = None, limit: int = 20) -> dict:
        return agent.describe_tool(tool_name, section, path, cursor, limit)

    @server.tool(description=DESCRIPTIONS['call_tool'])
    def call_tool(tool_name: str, arguments: dict, request_id: str):
        import json

        from mcp.types import CallToolResult, TextContent
        try:
            result = agent.call_tool(tool_name, arguments, request_id)
        except ToolError as exc:
            result = {'status': 'rejected', 'error': {'code': exc.code, 'message': exc.message}}
        return CallToolResult(
            content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
            structuredContent=result, isError=result['status'] in {'rejected', 'error', 'unknown'})

    @server.tool(description=DESCRIPTIONS['read_result'])
    def read_result(result_ref: str, path: str = '/data', cursor: str | None = None, limit: int = 20) -> dict:
        return agent.read_result(result_ref, path, cursor, limit)

    @server.tool(description=DESCRIPTIONS['query_result'])
    def query_result(result_ref: str, path: str = '/data', keys: list[str] | None = None,
                     query: str = '', fields: dict[str, str] | None = None,
                     where: list[dict] | None = None, sort: dict | None = None,
                     aggregates: list[dict] | None = None, cursor: str | None = None, limit: int = 50) -> dict:
        return dispatch(agent, 'query_result', {k: v for k, v in dict(result_ref=result_ref, path=path,
            keys=keys, query=query, fields=fields, where=where, sort=sort,
            aggregates=aggregates, cursor=cursor, limit=limit).items() if v is not None})

    return server
