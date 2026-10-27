"""Reusable native document construction from captured Frappe metadata.

This layer preserves child documents and native references. Catalog projection
is a separate concern: unsupported structures must not be flattened into fake
app fields. Server controllers remain authoritative for business validity.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from mac_bench.environments.apps.frappe.client import (
    FrappeClient,
    MetadataCapture,
)
from mac_bench.environments.errors import CompileFailure

_LAYOUT = {'Section Break', 'Column Break', 'Tab Break', 'HTML', 'Button', 'Heading', 'Fold'}
_TEXT = {'Data', 'Small Text', 'Text', 'Long Text', 'Text Editor', 'Markdown Editor',
         'Code', 'Select', 'Link', 'Dynamic Link', 'Attach', 'Attach Image',
         'Color', 'Icon', 'Barcode', 'Autocomplete', 'Password', 'Read Only',
         'HTML Editor', 'Signature', 'Geolocation'}
_TABLE = {'Table', 'Table MultiSelect'}


class FrappeDocumentFactory:
    """Create one valid native aggregate, with already-created parent records.

    Does not invent defaults, auto-create parents, or suppress mandatory fields.
    Frappe applies naming/defaults/fetches and controller validation on insert.
    A submitted baseline explicitly asks for native submission after insertion.
    """

    def __init__(self, client: FrappeClient, metadata: MetadataCapture, doctype: str):
        self.client = client
        self.metadata = metadata
        self.doctype = doctype
        self.schema = metadata.doctype(doctype)
        if self.schema.get('istable') or self.schema.get('issingle') or self.schema.get('is_virtual'):
            raise CompileFailure('frappe_factory_requires_stored_root_doctype')

    def validate(self, values: dict) -> None:
        self._validate(self.doctype, values, ())

    def _validate(self, doctype, values, ancestry):
        if not isinstance(values, dict) or doctype in ancestry:
            raise CompileFailure('frappe_factory_invalid_or_recursive_document')
        meta = self.metadata.doctype(doctype)
        fields = {field['fieldname']: field for field in meta['fields']
                  if field['fieldtype'] not in _LAYOUT}
        if set(values) - set(fields):
            raise CompileFailure('frappe_factory_unknown_native_field')
        for name, value in values.items():
            field = fields[name]
            if field.get('read_only'):
                raise CompileFailure('frappe_factory_read_only_field')
            kind = field['fieldtype']
            if value is None:
                if field.get('reqd'):
                    raise CompileFailure('frappe_factory_null_required_field')
                continue
            if kind in _TABLE:
                if not isinstance(value, list):
                    raise CompileFailure('frappe_factory_child_table_requires_list')
                for row in value:
                    self._validate(field['options'], row, (*ancestry, doctype))
            elif kind in _TEXT:
                if not isinstance(value, str):
                    raise CompileFailure('frappe_factory_text_requires_string')
                if kind == 'Select' and value not in (field.get('options') or '').split('\n'):
                    raise CompileFailure('frappe_factory_invalid_select_option')
            elif kind == 'Check':
                if type(value) not in {int, bool} or value not in (0, 1):
                    raise CompileFailure('frappe_factory_invalid_check')
            elif kind == 'Int':
                if type(value) is not int:
                    raise CompileFailure('frappe_factory_invalid_integer')
            elif kind in {'Float', 'Currency', 'Percent', 'Duration', 'Rating'}:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise CompileFailure('frappe_factory_invalid_decimal')
                try:
                    number = Decimal(str(value))
                except InvalidOperation:
                    raise CompileFailure('frappe_factory_invalid_decimal') from None
                if not number.is_finite():
                    raise CompileFailure('frappe_factory_nonfinite_decimal')
            elif kind in {'Date', 'Datetime', 'Time'}:
                try:
                    {'Date': date, 'Datetime': datetime, 'Time': time}[kind].fromisoformat(value)
                except (TypeError, ValueError):
                    raise CompileFailure('frappe_factory_invalid_temporal_value') from None
            elif kind == 'JSON':
                # Serialization and native controller determine acceptance.
                pass
            else:
                raise CompileFailure(f'frappe_factory_unsupported_field_type:{kind}')
        # Missing required values are deliberately left to the server: defaults,
        # fetch_from, naming and before_validate controllers may provide them.
        # This local validator never claims schema acceptance proves validity.

    def references(self, values: dict) -> tuple[tuple[str, str], ...]:
        self.validate(values)
        result = set()

        def visit(doctype, record):
            for field in self.metadata.doctype(doctype)['fields']:
                value = record.get(field['fieldname'])
                if value is None or value == '':
                    continue
                kind = field['fieldtype']
                if kind in _TABLE:
                    for row in value:
                        visit(field['options'], row)
                elif kind == 'Link':
                    target = field.get('options')
                    if not isinstance(target, str) or not target:
                        raise CompileFailure('frappe_factory_link_target_missing')
                    result.add((target, value))
                elif kind == 'Dynamic Link':
                    target = record.get(field.get('options'))
                    if not isinstance(target, str) or not target:
                        raise CompileFailure('frappe_factory_dynamic_link_target_missing')
                    result.add((target, value))

        visit(self.doctype, values)
        return tuple(sorted(result))

    def create(self, values: dict, *, submitted: bool = False, name: str | None = None) -> dict:
        if submitted and not self.schema.get('is_submittable'):
            raise CompileFailure('frappe_factory_doctype_not_submittable')
        prompted = (self.schema.get('autoname') or '').lower() == 'prompt'
        if prompted != (name is not None):
            raise CompileFailure('frappe_factory_name_must_match_native_naming_rule')
        if not self.client.has_permission(self.doctype, 'create'):
            raise CompileFailure('frappe_factory_create_permission_denied')
        for parent_doctype, parent_name in self.references(values):
            self.client.read(parent_doctype, parent_name)
        document = self.client.create(self.doctype, deepcopy(values), name=name)
        if document.get('docstatus') != 0:
            raise CompileFailure('frappe_factory_new_document_not_draft')
        if prompted and document['name'] != name:
            raise CompileFailure('frappe_factory_prompted_name_readback_mismatch')
        return self.client.submit(document) if submitted else document

    def amend(self, cancelled: dict, values: dict, *, submitted: bool = False) -> dict:
        """Insert a replacement with native amendment lineage and naming.

        Values are the complete intended new business document, including child
        rows. No previous financial totals, workflow states or child identities
        are blindly copied. The original stays cancelled. Attachment/version
        side effects belong to the native controller and need inventory readback
        before any compiler contract can claim their complete effect footprint.
        """
        self.client._observed(cancelled)
        if (cancelled['doctype'] != self.doctype or cancelled.get('docstatus') != 2
                or not self.schema.get('is_submittable')):
            raise CompileFailure('frappe_amend_requires_cancelled_submittable_document')
        lineage = next((field for field in self.schema['fields']
                        if field['fieldname'] == 'amended_from'), None)
        if lineage is None or lineage['fieldtype'] != 'Link' or lineage.get('options') != self.doctype:
            raise CompileFailure('frappe_amend_native_lineage_field_missing')
        if not isinstance(values, dict) or 'amended_from' in values:
            raise CompileFailure('frappe_amend_lineage_is_operation_owned')
        parents = self.references(values)
        current = self.client.read(self.doctype, cancelled['name'])
        if current.get('docstatus') != 2 or current.get('modified') != cancelled['modified']:
            raise CompileFailure('frappe_amend_observation_stale')
        for action in ('create', 'amend'):
            if not self.client.has_permission(self.doctype, action,
                                              '' if action == 'create' else cancelled['name']):
                raise CompileFailure(f'frappe_amend_permission_denied:{action}')
        for parent_doctype, parent_name in parents:
            self.client.read(parent_doctype, parent_name)
        payload = {**deepcopy(values), 'amended_from': cancelled['name']}
        document = self.client.create(self.doctype, payload)
        if (document['name'] == cancelled['name'] or document.get('docstatus') != 0
                or document.get('amended_from') != cancelled['name']):
            raise CompileFailure('frappe_amend_readback_invalid')
        return self.client.submit(document) if submitted else document
