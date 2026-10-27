"""Build reusable operations from Entity Catalog, without factories or LLM calls."""
import re
from hashlib import sha256

from jsonschema import Draft202012Validator

from mac_bench.construction.realize.catalog.factories.assembly import BuildContext
from mac_bench.construction.realize.tools.adapters import frappe, rest, twenty
from mac_bench.construction.realize.tools.contracts import Tool, ToolError
from mac_bench.construction.realize.tools.examples import example
from mac_bench.construction.realize.tools.registry import ToolRegistry


def build_registry(entities, *, bindings=None):
    context = BuildContext(entities)
    selected = sorted(context.bindings if bindings is None else set(bindings))
    if set(selected) - context.bindings.keys():
        raise ValueError('Unknown native binding')
    # Some legacy task captures selected the calendar event/channel roots but
    # omitted their separately addressable participants. Restore only that
    # companion API in the same workspace; actor/native permissions still apply.
    companion = set(selected)
    for identity in selected:
        binding = context.bindings[identity]
        if binding['entity'] == 'twenty.calendar_event':
            matches = [key for key, value in context.bindings.items()
                       if value['entity'] == 'twenty.calendar_event_participant'
                       and value['scope'] == binding['scope']]
            if len(matches) == 1:
                companion.add(matches[0])
    selected = sorted(companion)
    tools, exclusions = [], []
    for identity in selected:
        binding = context.bindings[identity]
        entity = binding['entity']
        card, contract = context.cards[entity], context.contracts[identity]
        app = card['application']
        # Catalog roles describe semantic organization, not native addressability
        # or actor permissions. Relations such as Twenty TaskTarget have their
        # own CRUD roots; HRMS Event has a normal document API. Keep declared
        # operations for every role and let native contracts and session roles
        # determine execution. Owned rows without an API remain parent-managed.
        if not contract['operations']:
            exclusions.append({'binding': identity, 'reason': 'No standalone native operations; access through the owning resource.'})
            continue
        operations = dict(contract['operations'])
        if (app == 'hrms' and 'read' in operations
                and operations['read'].get('input', {}).get('read_locator') != 'singleton'):
            operations['list'] = {'contract_status': 'declared'}
        for operation, native in sorted(operations.items()):
            if native['contract_status'] != 'declared':
                exclusions.append({'binding': identity, 'operation': operation, 'reason': native['contract_status']})
                continue
            try:
                if app == 'hrms':
                    built = frappe.listing(context, binding) if operation == 'list' else frappe.operation(context, binding, operation, native)
                elif app == 'twenty':
                    built = twenty.operation(context, binding, operation, native)
                elif app == 'pretix':
                    built = rest.pretix_operation(context, binding, operation, native)
                else:
                    built = rest.bigcapital_operation(context, binding, operation, native)
                schema, handler, read_only, notes = built
                Draft202012Validator.check_schema(schema)
            except ToolError as exc:
                exclusions.append({'binding': identity, 'operation': operation, 'reason': exc.code})
                continue
            slug = re.sub(r'[^a-zA-Z0-9_-]', '_', operation)
            name = entity + '.' + slug
            if len(entities.variants(entity)) > 1 or sum(len(v['bindings']) for v in entities.variants(entity)) > 1:
                name += '.' + sha256(identity.encode()).hexdigest()[:10]
            if len(name) > 128:
                name = name[:110] + '.' + sha256(name.encode()).hexdigest()[:12]
            verb = operation.split('@')[0]
            purpose = {
                'read': 'Read one record and its current native state',
                'findOne': 'Find one record using native filters',
                'list': 'List records with native filtering and pagination',
                'findMany': 'Search records with native filters and cursor pagination',
                'create': 'Create a new record using native validation and defaults',
                'createOne': 'Create one record using native validation and defaults',
                'update': 'Change supplied fields of an existing record',
                'updateOne': 'Change supplied fields of one existing record',
                'partial_update': 'Change supplied fields of an existing record',
                'submit': 'Submit a draft through the native document lifecycle',
                'cancel': 'Cancel a submitted document through its native lifecycle',
                'delete': 'Delete the specified record subject to native link and permission checks',
                'destroy': 'Delete the specified record subject to native link and permission checks',
            }.get(verb, re.sub(r'([a-z])([A-Z])', r'\1 \2', verb).replace('_', ' ').capitalize())
            description = f'{purpose}: {card["label"]} ({app}).'
            public_fields = {k: {p: v[p] for p in ('description', 'label', 'native_type', 'reference',
                'constraints', 'write', 'native_conditions', 'dependencies') if p in v}
                for k, v in contract['fields'].items()}
            output = twenty.response_contract(context, binding, operation, native) if app == 'twenty' else {
                'native_response': native.get('response', {}), 'fields': public_fields,
                'identity': contract['identity']}
            result_description = {
                'hrms': 'data is a native document, or a list of selected fields for list. Preserve name/modified/docstatus. pagination.next_offset continues lists.',
                'twenty': 'data is the selected root result. Connections contain edges[].node and pageInfo; select pageInfo fields to continue. See output for valid response paths.',
                'pretix': 'data retains the native response. Lists use results/count/next; continue using this operation and native query parameters, not a next URL.',
                'bigcapital': 'data retains native response keys. See output for the captured shape; query paging and mutation return types depend on the registered operation.',
            }[app]
            tools.append(Tool(name, app, entity, operation, identity, binding['scope'], description,
                schema, result_description,
                read_only, handler, notes, {'identity': contract['identity'],
                    'response': native.get('response', {}),
                    'fields': public_fields, 'output': output, 'examples': example(schema),
                    'operation_contract': native,
                    'catalog_revision': entities.revision,
                    'execution_verified': False}))
    return ToolRegistry(tools, catalog_revision=entities.revision, exclusions=exclusions)
