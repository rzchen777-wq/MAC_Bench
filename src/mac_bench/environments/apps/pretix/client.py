"""Native Pretix resources and order lifecycle, without a scenario projection.

Orders retain heterogeneous positions, variations, fees, invoice addresses,
payments and refunds. Monetary decimal strings are never coerced into a fixed
currency scale. This protocol layer is not a complete Catalog binding.

Protocol reference: https://docs.pretix.eu/dev/api/resources/orders.html
Deployment-specific serializers, plugins and permissions remain authoritative.
"""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import quote, urlencode

from mac_bench.environments.errors import CompileFailure
from mac_bench.environments.runtime.infrastructure.transport import RestTransport


def _identity(value):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value) or (isinstance(value, int) and value < 1)):
        raise CompileFailure('pretix_invalid_native_identity')
    return quote(str(value), safe='')


class PretixChangeReadbackRequired(CompileFailure):
    def __init__(self, response, before, positions, orders):
        super().__init__('pretix_change_requires_reconciliation_do_not_retry')
        self.action_response = deepcopy(response)
        self.order_before = deepcopy(before)
        self.positions_after = deepcopy(positions)
        self.orders_after = deepcopy(orders)


class PretixClient:
    def __init__(self, transport: RestTransport, organizer: str):
        self.transport = transport
        self.root = 'api/v1/organizers/' + _identity(organizer) + '/events/'

    def path(self, event: str, suffix: str = '') -> str:
        return self.root + _identity(event) + '/' + suffix

    def listing(self, path: str, *, identity: str, max_records: int = 100000,
                max_pages: int = 10000) -> list[dict]:
        if (type(max_records) is not int or max_records < 1
                or type(max_pages) is not int or max_pages < 1):
            raise ValueError('invalid inventory bound')
        rows, pages, identities = [], set(), set()
        expected_count = None
        while path:
            if not isinstance(path, str) or path in pages:
                raise CompileFailure('pretix_pagination_invalid_or_cyclic')
            pages.add(path)
            if len(pages) > max_pages:
                raise CompileFailure('pretix_pagination_page_limit_exceeded')
            # RestTransport rejects pagination redirects to a different origin.
            page = self.transport.request('GET', path)
            if (not isinstance(page, dict) or not isinstance(page.get('results'), list)
                    or 'next' not in page):
                raise CompileFailure('pretix_pagination_envelope_invalid')
            count = page.get('count')
            if type(count) is not int or count < 0 or count > max_records:
                raise CompileFailure('pretix_pagination_count_invalid_or_exceeds_limit')
            if expected_count is None:
                expected_count = count
            elif count != expected_count:
                raise CompileFailure('pretix_pagination_count_changed')
            for row in page['results']:
                if not isinstance(row, dict) or identity not in row:
                    raise CompileFailure('pretix_inventory_identity_missing')
                key = row[identity]
                _identity(key)
                if key in identities:
                    raise CompileFailure('pretix_inventory_identity_duplicate')
                identities.add(key)
                rows.append(deepcopy(row))
                if len(rows) > max_records:
                    raise CompileFailure('pretix_inventory_limit_exceeded')
            path = page['next']
            if path is not None and (not isinstance(path, str) or not path):
                raise CompileFailure('pretix_next_page_invalid')
        if len(rows) != expected_count:
            raise CompileFailure('pretix_pagination_incomplete')
        return rows

    def read_event(self, event: str) -> dict:
        return self._read(self.path(event), 'slug', event)

    def create_event(self, values: dict) -> dict:
        return self._create(self.root, values, 'slug')

    def read_resource(self, event: str, collection: str, identity: int) -> dict:
        return self._read(self._collection(event, collection) + _identity(identity) + '/', 'id', identity)

    def create_resource(self, event: str, collection: str, values: dict) -> dict:
        return self._create(self._collection(event, collection), values, 'id')

    def update_resource(self, event: str, collection: str, identity: int, values: dict) -> dict:
        path = self._collection(event, collection) + _identity(identity) + '/'
        self._payload(values)
        self.transport.request('PATCH', path, deepcopy(values))
        return self._read(path, 'id', identity)

    def _collection(self, event, collection):
        if collection not in {'items', 'quotas', 'subevents', 'categories', 'taxrules'}:
            raise CompileFailure('pretix_resource_collection_not_implemented')
        return self.path(event, collection + '/')

    def _item_children(self, event, item, collection):
        if collection not in {'variations', 'addons', 'bundles', 'program_times'}:
            raise CompileFailure('pretix_item_child_collection_not_implemented')
        return self.path(event, 'items/' + _identity(item) + '/' + collection + '/')

    def read_item_child(self, event, item, collection, identity):
        path = self._item_children(event, item, collection) + _identity(identity) + '/'
        return self._read(path, 'id', identity)

    def create_item_child(self, event, item, collection, values):
        return self._create(self._item_children(event, item, collection), values, 'id')

    def update_item_child(self, event, item, collection, identity, values):
        self._payload(values)
        path = self._item_children(event, item, collection) + _identity(identity) + '/'
        self.transport.request('PATCH', path, deepcopy(values))
        return self._read(path, 'id', identity)

    def read_order(self, event: str, code: str) -> dict:
        query = urlencode({'include_canceled_positions': 'true', 'include_canceled_fees': 'true'})
        return self._read(self._order(event, code) + '?' + query, 'code', code)

    def create_order(self, event: str, values: dict) -> dict:
        self._payload(values)
        if not isinstance(values.get('positions'), list) or not values['positions']:
            raise CompileFailure('pretix_order_requires_explicit_positions')
        # Each position is provided independently. The server validates items,
        # variations, add-ons, pricing, tax and capacity; no homogeneous shortcut.
        response = self.transport.request('POST', self.path(event, 'orders/'), deepcopy(values))
        code = self._response_identity(response, 'code')
        return self.read_order(event, code)

    def change_order(self, event: str, code: str, changes: dict) -> dict:
        """Retain newly produced orders when native positions are split off.

        Native split moves positions without replacing their global IDs. Its
        response describes the original order only, so discover destination
        orders by independently reading those positions. Financial/invoice
        side effects still require their own completeness checks.
        """
        self._payload(changes)
        changes = deepcopy(changes)
        before = self.read_order(event, code)
        positions = before.get('positions')
        if not isinstance(positions, list):
            raise CompileFailure('pretix_change_before_positions_missing')
        known = {self._response_identity(position, 'id'): position for position in positions}

        def requested_positions(key):
            entries = changes.get(key, [])
            if not isinstance(entries, list):
                raise CompileFailure('pretix_change_position_actions_invalid')
            identities = set()
            for entry in entries:
                identity = self._response_identity(entry, 'position')
                if identity in identities or identity not in known:
                    raise CompileFailure('pretix_change_position_duplicate_or_not_in_order')
                identities.add(identity)
            return identities

        # Native controller ignores split requests for positions canceled in
        # the same transaction. Do not expect these to acquire a new parent.
        split_ids = requested_positions('split_positions') - requested_positions('cancel_positions')
        response = self.transport.request('POST', self._order(event, code) + 'change/', changes)
        moved, orders = {}, {}
        try:
            destinations = {code}
            for identity in sorted(split_ids, key=str):
                position = self.read_position(event, identity)
                moved[identity] = position
                destination = self._response_identity(position, 'order')
                if destination == code:
                    raise CompileFailure('pretix_split_position_not_moved')
                destinations.add(destination)
            for destination in sorted(destinations):
                orders[destination] = self.order_inventory(event, destination)
            for identity, position in moved.items():
                destination = position['order']
                target_positions = orders[destination]['order'].get('positions')
                if (not isinstance(target_positions, list)
                        or sum(isinstance(item, dict) and item.get('id') == identity
                               for item in target_positions) != 1):
                    raise CompileFailure('pretix_split_destination_position_missing')
        except Exception:
            raise PretixChangeReadbackRequired(response, before, moved, orders) from None
        return {'action_response': deepcopy(response), 'order_before': before,
                'positions_after': moved, 'orders_after': orders}

    def read_position(self, event: str, identity: int) -> dict:
        return self._read(self.path(event, 'orderpositions/' + _identity(identity) + '/'), 'id', identity)

    def cancel_order(self, event: str, code: str, options: dict) -> dict:
        self._payload(options)
        self.transport.request('POST', self._order(event, code) + 'mark_canceled/', deepcopy(options))
        # Cancellation fees can affect the resulting native order status. Do not
        # replace the actual response with a universal status='c' postcondition.
        return self.read_order(event, code)

    def read_transaction(self, event: str, code: str, collection: str, local_id: int) -> dict:
        path = self._transactions(event, code, collection) + _identity(local_id) + '/'
        return self._read(path, 'local_id', local_id)

    def create_transaction(self, event: str, code: str, collection: str, values: dict) -> dict:
        """Record a native payment/refund; this alone does not execute a gateway.

        Provider-specific policy and amount validation must be supplied by its
        lifecycle binding. A manually entered record is never proof of settlement.
        """
        return self._create(self._transactions(event, code, collection), values, 'local_id')

    def transaction_action(self, event: str, code: str, collection: str, local_id: int,
                           action: str, options: dict) -> dict:
        allowed = {'payments': {'confirm', 'cancel', 'refund'},
                   'refunds': {'done', 'process', 'cancel'}}
        if action not in allowed.get(collection, set()):
            raise CompileFailure('pretix_transaction_action_not_implemented')
        self._payload(options)
        path = self._transactions(event, code, collection) + _identity(local_id) + '/'
        response = self.transport.request('POST', path + action + '/', deepcopy(options))
        if collection == 'payments' and action == 'refund':
            # This endpoint returns a NEW refund, not the original payment.
            refund_id = self._response_identity(response, 'local_id')
            return self.read_transaction(event, code, 'refunds', refund_id)
        return self.read_transaction(event, code, collection, local_id)

    def read_invoice(self, event: str, number: str) -> dict:
        invoice = self._read(self.path(event, 'invoices/' + _identity(number) + '/'), 'number', number)
        if 'event' in invoice and invoice['event'] != event:
            raise CompileFailure('pretix_invoice_event_mismatch')
        return invoice

    def invoice_inventory(self, event: str, code: str, *, max_records: int = 100000) -> dict:
        """Read order invoices plus their explicit reference closure.

        Keeps original and cancellation documents, lines and transmission states.
        A reference may lead outside the requested order; retain that distinction
        instead of silently treating every referenced invoice as this order's.
        Does not download files or initiate invoice transmission.
        """
        path = self.path(event, 'invoices/') + '?' + urlencode({'order': code})
        listed = self.listing(path, identity='number', max_records=max_records)
        invoices, references = {}, {}
        pending = []
        for row in listed:
            number = self._response_identity(row, 'number')
            invoice = self.read_invoice(event, number)
            if invoice.get('order') != code:
                raise CompileFailure('pretix_invoice_order_scope_mismatch')
            invoices[number] = invoice
            pending.append(invoice)
        while pending:
            invoice = pending.pop()
            if 'refers' not in invoice or type(invoice.get('is_cancellation')) is not bool:
                raise CompileFailure('pretix_invoice_lineage_fields_missing')
            number = invoice['refers']
            if number is None:
                continue
            if not isinstance(number, str) or not number:
                raise CompileFailure('pretix_invoice_reference_invalid')
            if number in invoices or number in references:
                continue
            if len(invoices) + len(references) >= max_records:
                raise CompileFailure('pretix_invoice_reference_limit_exceeded')
            target = self.read_invoice(event, number)
            references[number] = target
            pending.append(target)
        return {'invoices': invoices, 'referenced_invoices': references}

    def order_inventory(self, event: str, code: str, *, max_records: int = 100000) -> dict:
        """Two observations of the native order, settlements and invoice lineage.

        Bounds apply to each collection. Stable observations do not establish
        queue completion, external settlement or actual invoice delivery.
        """
        def capture():
            order = self.read_order(event, code)
            result = {'order': order}
            for collection in ('payments', 'refunds'):
                rows = self.listing(self._transactions(event, code, collection),
                                    identity='local_id', max_records=max_records)
                result[collection] = {row['local_id']: self.read_transaction(
                    event, code, collection, row['local_id']) for row in rows}
            result.update(self.invoice_inventory(event, code, max_records=max_records))
            if self.read_order(event, code) != order:
                raise CompileFailure('pretix_order_changed_during_inventory')
            return result
        first, second = capture(), capture()
        if first != second:
            raise CompileFailure('pretix_order_inventory_changed_during_capture')
        return second

    def _order(self, event, code):
        return self.path(event, 'orders/' + _identity(code) + '/')

    def _transactions(self, event, code, collection):
        if collection not in {'payments', 'refunds'}:
            raise CompileFailure('pretix_transaction_collection_invalid')
        return self._order(event, code) + collection + '/'

    def _create(self, path, values, identity_field):
        self._payload(values)
        response = self.transport.request('POST', path, deepcopy(values))
        identity = self._response_identity(response, identity_field)
        return self._read(path + _identity(identity) + '/', identity_field, identity)

    def _read(self, path, field, identity):
        response = self.transport.request('GET', path)
        if self._response_identity(response, field) != identity:
            raise CompileFailure('pretix_readback_identity_mismatch')
        return deepcopy(response)

    @staticmethod
    def _response_identity(response, field):
        if not isinstance(response, dict) or field not in response:
            raise CompileFailure('pretix_response_identity_missing')
        _identity(response[field])
        return response[field]

    @staticmethod
    def _payload(values):
        if not isinstance(values, dict):
            raise CompileFailure('pretix_payload_requires_object')
