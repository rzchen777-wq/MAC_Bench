"""Frappe document protocol, shared by HRMS and other Frappe applications.

Metadata is captured from the installed site, including custom fields and child
DocTypes. It is not a replacement for server controllers or permission checks.
No action bypasses validation, writes docstatus directly, or retries a mutation.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import quote, urlencode

from mac_bench.environments.errors import CompileFailure
from mac_bench.environments.runtime.infrastructure.transport import RestTransport


def _segment(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CompileFailure('frappe_empty_document_identity')
    return quote(value, safe='')


@dataclass(frozen=True)
class MetadataCapture:
    """Effective site metadata; deployment identity must be recorded separately."""

    source: str
    digest: str
    documents: tuple[dict, ...]

    def doctype(self, name: str) -> dict:
        matches = [doc for doc in self.documents if doc.get('name') == name]
        # getdoctype emits a child definition for each table field, including
        # when earnings/deductions or several approver fields share one type.
        # Preserve that native response in the archive and its digest; a lookup
        # is unambiguous only when every occurrence has identical content.
        if not matches or any(doc != matches[0] for doc in matches[1:]):
            raise CompileFailure('frappe_metadata_missing_or_conflicting_doctype')
        return deepcopy(matches[0])

    def relations(self) -> tuple[dict, ...]:
        """Retain native static, dynamic, and child-table relation semantics."""
        result = []
        for doc in self.documents:
            for field in doc.get('fields', []):
                kind = field.get('fieldtype')
                if kind in {'Link', 'Dynamic Link', 'Table', 'Table MultiSelect'}:
                    result.append({
                        'doctype': doc['name'], 'field': field['fieldname'],
                        'kind': kind, 'options': field.get('options'),
                        'required': bool(field.get('reqd')),
                        'read_only': bool(field.get('read_only')),
                        'allow_on_submit': bool(field.get('allow_on_submit')),
                    })
        return tuple(result)


class FrappeClient:
    def __init__(self, transport: RestTransport):
        self.transport = transport

    def principal(self) -> str:
        response = self.transport.request('GET', 'api/method/frappe.auth.get_logged_user')
        user = response.get('message') if isinstance(response, dict) else None
        if not isinstance(user, str) or not user or user == 'Guest':
            raise CompileFailure('frappe_authenticated_principal_missing')
        return user

    def metadata(self, doctype: str) -> MetadataCapture:
        path = 'api/method/frappe.desk.form.load.getdoctype?' + urlencode({'doctype': doctype})
        response = self.transport.request('GET', path)
        return self.decode_metadata(doctype, response)

    def decode_metadata(self, doctype: str, response: dict) -> MetadataCapture:
        """Decode an actual API response, including captures fetched in a batch.

        This performs the identical structural validation used by metadata().
        It does not fetch data or turn installed database metadata into an API
        response. The collection caller retains HTTP/deployment provenance.
        """
        _segment(doctype)
        path = 'api/method/frappe.desk.form.load.getdoctype?' + urlencode({'doctype': doctype})
        docs = response.get('docs') if isinstance(response, dict) else None
        if not isinstance(docs, list) or not docs:
            raise CompileFailure('frappe_metadata_envelope_invalid')
        definitions = {}
        for doc in docs:
            if (not isinstance(doc, dict) or doc.get('doctype') != 'DocType'
                    or not isinstance(doc.get('name'), str) or not doc['name']
                    or not isinstance(doc.get('fields'), list)):
                raise CompileFailure('frappe_metadata_document_invalid')
            previous = definitions.get(doc['name'])
            if previous is not None and previous != doc:
                raise CompileFailure('frappe_metadata_conflicting_doctype_definitions')
            definitions[doc['name']] = doc
            fieldnames = set()
            for field in doc['fields']:
                if (not isinstance(field, dict) or not isinstance(field.get('fieldname'), str)
                        or not isinstance(field.get('fieldtype'), str)
                        or field['fieldname'] in fieldnames):
                    raise CompileFailure('frappe_metadata_field_invalid')
                fieldnames.add(field['fieldname'])
        if doctype not in definitions:
            raise CompileFailure('frappe_requested_metadata_missing')
        for doc in docs:
            for field in doc['fields']:
                if field['fieldtype'] in {'Table', 'Table MultiSelect'} and field.get('options') not in definitions:
                    raise CompileFailure('frappe_child_metadata_missing')
        encoded = json.dumps(docs, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        return MetadataCapture(self.transport.base + path, sha256(encoded).hexdigest(),
                               tuple(deepcopy(docs)))

    def read(self, doctype: str, name: str) -> dict:
        path = f'api/resource/{_segment(doctype)}/{_segment(name)}'
        response = self.transport.request('GET', path)
        return self._document(response, 'data', doctype, name)

    def list_names(self, doctype: str, *, filters: list | dict, page_size: int = 100,
                   max_documents: int = 100000) -> tuple[str, ...]:
        """Inventory visible records; caller must quiesce for a stable snapshot.

        No unbounded/partial inventory is returned as a complete world. Filters
        are explicit because a site may also contain unrelated benchmark data.
        """
        if not 1 <= page_size <= 1000 or max_documents < 1:
            raise ValueError('invalid inventory limits')
        names = []
        seen = set()
        while True:
            query = urlencode({'fields': json.dumps(['name']), 'filters': json.dumps(filters),
                               'order_by': 'name asc', 'limit_start': len(names),
                               'limit_page_length': page_size})
            response = self.transport.request('GET', f'api/resource/{_segment(doctype)}?{query}')
            rows = response.get('data') if isinstance(response, dict) else None
            if not isinstance(rows, list):
                raise CompileFailure('frappe_inventory_envelope_invalid')
            if not rows:
                return tuple(names)
            for row in rows:
                name = row.get('name') if isinstance(row, dict) else None
                if not isinstance(name, str) or not name or name in seen:
                    raise CompileFailure('frappe_inventory_identity_invalid_or_unstable')
                names.append(name)
                seen.add(name)
                if len(names) > max_documents:
                    raise CompileFailure('frappe_inventory_limit_exceeded')

    def has_permission(self, doctype: str, action: str, name: str = '') -> bool:
        if action not in {'read', 'write', 'create', 'submit', 'cancel', 'delete', 'amend'}:
            raise ValueError('unsupported permission action')
        query = urlencode({'doctype': doctype, 'docname': name, 'perm_type': action})
        response = self.transport.request('GET', 'api/method/frappe.client.has_permission?' + query)
        result = response.get('message') if isinstance(response, dict) else None
        value = result.get('has_permission') if isinstance(result, dict) else None
        if type(value) not in {bool, int} or value not in (0, 1):
            raise CompileFailure('frappe_permission_response_invalid')
        return bool(value)

    def create(self, doctype: str, values: dict, *, name: str | None = None) -> dict:
        self._business_values(values)
        payload = deepcopy(values)
        if name is not None:
            _segment(name)
            payload['name'] = name
        response = self.transport.request('POST', f'api/resource/{_segment(doctype)}',
                                          payload)
        created = self._document(response, 'data', doctype)
        return self.read(doctype, created['name'])

    def save(self, before: dict, changes: dict) -> dict:
        """Send the observed modified timestamp through native save concurrency checks."""
        self._business_values(changes)
        self._observed(before)
        doc = deepcopy(before)
        doc.update(deepcopy(changes))
        self._call('save', {'doc': json.dumps(doc)}, before['doctype'], before['name'])
        return self.read(before['doctype'], before['name'])

    def submit(self, before: dict) -> dict:
        self._observed(before)
        if before.get('docstatus') != 0:
            raise CompileFailure('frappe_submit_requires_draft')
        self._call('submit', {'doc': json.dumps(before)}, before['doctype'], before['name'])
        result = self.read(before['doctype'], before['name'])
        if result.get('docstatus') != 1:
            raise CompileFailure('frappe_submit_readback_not_submitted')
        return result

    def cancel(self, before: dict) -> dict:
        self._observed(before)
        if before.get('docstatus') != 1:
            raise CompileFailure('frappe_cancel_requires_submitted')
        # The native cancel endpoint accepts only identity, not modified. This
        # detects stale input but is not an atomic concurrency guarantee; the
        # deployment must exclude concurrent writers during materialization.
        current = self.read(before['doctype'], before['name'])
        if current.get('modified') != before['modified']:
            raise CompileFailure('frappe_cancel_observation_stale')
        self._call('cancel', {'doctype': before['doctype'], 'name': before['name']},
                   before['doctype'], before['name'])
        result = self.read(before['doctype'], before['name'])
        if result.get('docstatus') != 2:
            raise CompileFailure('frappe_cancel_readback_not_cancelled')
        return result

    def _call(self, method: str, arguments: dict, doctype: str, name: str) -> dict:
        response = self.transport.request('POST', 'api/method/frappe.client.' + method, arguments)
        return self._document(response, 'message', doctype, name)

    @staticmethod
    def _document(response, envelope, doctype, name=None):
        doc = response.get(envelope) if isinstance(response, dict) else None
        if (not isinstance(doc, dict) or doc.get('doctype') != doctype
                or not isinstance(doc.get('name'), str) or not doc['name']
                or (name is not None and doc['name'] != name)):
            raise CompileFailure('frappe_document_response_invalid')
        return deepcopy(doc)

    @staticmethod
    def _observed(doc):
        if (not isinstance(doc, dict) or not all(isinstance(doc.get(key), str) and doc[key]
                                               for key in ('doctype', 'name', 'modified'))):
            raise CompileFailure('frappe_mutation_requires_observed_document')

    @staticmethod
    def _business_values(values):
        protected = {'doctype', 'name', 'docstatus', 'modified', 'modified_by', 'creation',
                     'owner', 'flags', 'ignore_permissions', 'ignore_links', 'ignore_mandatory'}
        if not isinstance(values, dict) or protected.intersection(values):
            raise CompileFailure('frappe_managed_document_field_write')
