"""Immutable tool discovery with complete, revision-pinned pagination."""
import re
from difflib import get_close_matches
from copy import deepcopy
from hashlib import sha256

from mac_bench.environments.catalog.entities.errors import CatalogError
from mac_bench.environments.catalog.entities.pagination import paginate
from mac_bench.environments.tools.contracts import ToolError, encoded
from mac_bench.environments.tools.documentation import describe

APPS = {
    'pretix': 'Event registration, tickets, orders, payments and refunds.',
    'hrms': 'Employees, qualifications, calendars and staff shift assignments.',
    'twenty': 'CRM companies and contacts, notes, tasks, calendars, participants and their relationships.',
    'bigcapital': 'Accounting accounts, bills, payments and journal entries.',
}


def search_text(value):
    """Use the same word boundaries for native camelCase and catalog snake_case."""
    value = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', value)
    return re.sub(r'[_.-]+', ' ', value).casefold()


class ToolRegistry:
    def __init__(self, tools, *, catalog_revision, exclusions=()):
        self._tools = {}
        for tool in tools:
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', tool.name) or tool.name in self._tools:
                raise ValueError('Invalid or duplicate tool name: ' + tool.name)
            self._tools[tool.name] = tool
        self.catalog_revision = catalog_revision
        self.exclusions = deepcopy(list(exclusions))
        # Hash each full executable contract, without constructing a second
        # multi-megabyte registry document just to discover a small subset.
        self.revision = sha256(encoded({'interface': 2, 'catalog': catalog_revision,
            'tools': [(k, sha256(encoded(self._tools[k].archive()).encode()).hexdigest())
                      for k in sorted(self._tools)]}).encode()).hexdigest()

    def get(self, name):
        if name not in self._tools:
            candidates = get_close_matches(name, self._tools, n=4, cutoff=0.45)
            hint = (' Similar available tools: ' + ', '.join(candidates) + '.') if candidates else ''
            raise ToolError('tool_unavailable', 'Unknown tool name.' + hint
                            + ' Call list_tools(query="entity or action") to find an available name, '
                              'then describe_tool(tool_name=...) for its arguments.')
        return self._tools[name]

    def tools(self):
        return tuple(self._tools.values())

    def subset(self, predicate):
        return ToolRegistry((t for t in self._tools.values() if predicate(t)), catalog_revision=self.catalog_revision)

    def list_apps(self):
        apps = sorted({t.application for t in self._tools.values()})
        return {'revision': self.revision, 'apps': [
            {'name': app, 'description': APPS[app], 'tool_count': sum(t.application == app for t in self._tools.values())}
            for app in apps]}

    def list_tools(self, app=None, query=None, cursor=None, limit=20, entity=None, read_only=None):
        if app is not None and app not in APPS:
            raise ToolError('unknown_application', 'Unknown application.')
        words = search_text(query or '').split()
        rows = [t.summary() for _, t in sorted(self._tools.items()) if (app is None or t.application == app)
                and (entity is None or entity == t.entity) and (read_only is None or read_only == t.read_only)
                and all(w in search_text(t.name + ' ' + t.description) for w in words)]
        try:
            return paginate(rows, revision=self.revision, query=[app, query, entity, read_only], cursor=cursor, limit=limit, max_bytes=8000)
        except CatalogError as exc:
            raise ToolError('invalid_pagination', str(exc)) from None

    def describe_tool(self, tool_name, section='usage', path='', cursor=None, limit=20):
        return describe(self.get(tool_name), self.revision, section=section, path=path, cursor=cursor, limit=limit)
