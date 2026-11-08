"""Attributed native steps with explicit partial/uncertain outcomes."""
from copy import deepcopy

from .contracts import ToolError


class CompoundOperation:
    @property
    def applications(self):
        return frozenset(tool.application for tool in self.operations.values())

    def __call__(self, connection, arguments):
        raise RuntimeError('Compound operations require an attributed agent session')


class StepStopped(Exception):
    def __init__(self, receipt):
        self.receipt = receipt


class Steps:
    def __init__(self, agent, call_id, operations):
        self.agent, self.call_id, self.operations = agent, call_id, operations
        self.steps, self.wrote = [], False
        self.ids = {}

    def invoke(self, entity, operation, args):
        from hashlib import sha256
        tool = self.operations[entity, operation]
        request = 'step-' + sha256((self.call_id + ':' + str(len(self.steps))).encode()).hexdigest()
        result = self.agent.call_tool(tool.name, deepcopy(args), request)
        self.steps.append({'tool': tool.name, 'call_id': result['call_id'],
                           'status': result['status'], 'read_only': tool.read_only})
        if not tool.read_only and result['status'] != 'rejected':
            self.wrote = True
        if result['status'] != 'succeeded':
            raise StepStopped(result)
        if not result.get('data_complete', True):
            result, _ = self.agent.world.journal.receipt(self.agent.world.world_id,
                self.agent.actor, self.agent.role.name, result['call_id'])
        return result['data']

    def one(self, entity, identity, fields):
        result = self.invoke(entity, 'findOne', {'filter': {'id': {'eq': identity}}, 'fields': fields})
        if not isinstance(result, dict) or result.get('id') != identity:
            raise ToolError('record_not_found', 'The requested native record does not exist or is not visible.')
        return result

    def many(self, entity, filter_, fields):
        rows, cursor, seen = [], None, set()
        while True:
            args = {'filter': filter_, 'first': 100, 'fields': [*fields,
                'pageInfo.hasNextPage', 'pageInfo.endCursor']}
            if cursor:
                args['after'] = cursor
            result = self.invoke(entity, 'findMany', args)
            rows.extend(edge['node'] for edge in result['edges'])
            if not result.get('pageInfo', {}).get('hasNextPage'):
                return rows
            cursor = result['pageInfo'].get('endCursor')
            if not cursor or cursor in seen or len(rows) > 10000:
                raise ToolError('invalid_native_pagination', 'Native relation pagination did not advance within its bound.')
            seen.add(cursor)

    def confirmed_id(self, result, key):
        if not isinstance(result, dict) or not result.get('id'):
            raise ToolError('incomplete_native_response', 'The write did not return a confirmed ID. Inspect live state.', outcome='unknown')
        self.ids[key] = result['id']
        return result['id']

    def success(self, **data):
        return {'status': 'succeeded', 'data': {**self.ids, **data, 'steps': self.steps}, 'error': None}

    def failure(self, exc):
        if isinstance(exc, StepStopped):
            native = exc.receipt
            status = ('unknown' if native['status'] in {'unknown', 'accepted'} else
                      'partial' if self.wrote else 'error' if native['status'] == 'error' else 'rejected')
            error = {'code': 'native_steps_incomplete', 'native_status': native['status'],
                     'native_error': native.get('error')}
        else:
            status = 'unknown' if exc.outcome != 'not_started' else 'partial' if self.wrote else 'rejected'
            error = {'code': exc.code, 'message': exc.message}
        error['recovery'] = 'Inspect returned IDs, saved step receipts and live state. No write is automatically retried.'
        return {'status': status, 'data': {**self.ids, 'steps': self.steps}, 'error': error}


def native_operations(registry, entities):
    """Use only the actor's already permitted operations, in one native scope."""
    found = {}
    for tool in registry.tools():
        if tool.entity in entities:
            found.setdefault((tool.entity, tool.operation), []).append(tool)
    return {key: matches[0] for key, matches in found.items() if len(matches) == 1}
