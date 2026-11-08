"""Native business rejections retain actionable facts through the tool API."""
import json
from types import SimpleNamespace

import pytest

from mac_bench.environments.errors import NativeHTTPFailure
from mac_bench.environments.tools.error_feedback import feedback


def tool(app):
    return SimpleNamespace(application=app, native_contract={}, input_schema={}, read_only=False)


@pytest.mark.parametrize('kind,phrase,field', [
    ('INVOICES_HAS_NO_REMAINING_AMOUNT', 'outstanding amount', 'amount'),
    ('CREDIT_NOTE_HAS_NO_REMAINING_AMOUNT', 'remaining unused credit', 'amount'),
    ('INVOICES_IDS_NOT_FOUND', 'for this customer', 'invoiceId'),
    ('INVOICES_NOT_DELIVERED_YET', 'delivered', 'invoiceId'),
    ('CREDIT_NOTE_APPLY_TO_INVOICES_NOT_FOUND', 'already been removed', 'creditNoteAppliedInvoiceId'),
    ('VENDOR_CREDIT_APPLY_TO_BILLS_NOT_FOUND', 'already been removed', 'vendorCreditAppliedBillId'),
])
def test_credit_rejection_explains_specific_business_precondition(kind, phrase, field):
    payload = {'errors': [{'type': kind, 'message': 'SECRET native details',
                           'meta': {'database': 'SECRET_DB'}, 'stack': 'SECRET_STACK'}]}
    result = feedback(NativeHTTPFailure('POST', 400, payload), tool('bigcapital'))
    assert result['native_error_type'] == kind
    assert phrase in result['message']
    assert result['field_errors'][0]['field'] == field
    assert result['retry_guidance']
    assert 'SECRET' not in json.dumps(result)
    assert 'native_error_type' not in feedback(NativeHTTPFailure('POST', 400, payload), tool('pretix'))


def overlap_payload():
    message = ('Employee <strong>HR-EMP-00017</strong> already has an active Shift '
               '<strong>Condition check &amp; install</strong>: '
               '<a href="http://private-app/desk/shift-assignment/HR-SHA-26-11-00002?token=SECRET">'
               'HR-SHA-26-11-00002</a> that overlaps within this period.')
    return {'exc_type': 'OverlappingShiftError', 'exc': 'SECRET traceback',
            '_server_messages': json.dumps([json.dumps({'message': message,
                 'title': 'Overlapping Shifts', 'raise_exception': 1})]),
            'headers': {'Authorization': 'SECRET'}}


def test_frappe_double_encoded_overlap_keeps_record_locator_without_html_or_internals():
    result = feedback(NativeHTTPFailure('POST', 417, overlap_payload()), tool('hrms'))
    assert result['code'] == 'native_shift_overlap'
    assert result['conflict'] == {'employee': 'HR-EMP-00017',
        'shift_type': 'Condition check & install', 'doctype': 'Shift Assignment',
        'name': 'HR-SHA-26-11-00002'}
    assert result['field_errors'] == [{'field': 'employee', 'rule': 'conflict'}]
    assert 'request_id' in result['retry_guidance']
    assert not any(s in json.dumps(result) for s in ('SECRET', 'private-app', '<strong>', 'http://'))


def test_unrecognized_or_traceback_only_payload_does_not_invent_business_reason():
    payload = {'errors': [{'type': 'SECRET_UNKNOWN', 'message': 'SECRET'}]}
    result = feedback(NativeHTTPFailure('POST', 400, payload), tool('bigcapital'))
    assert result['code'] == 'native_request_rejected'
    assert 'native_error_type' not in result and 'SECRET' not in json.dumps(result)
    payload = overlap_payload()
    payload['exc'] = payload.pop('_server_messages')
    result = feedback(NativeHTTPFailure('POST', 417, payload), tool('hrms'))
    assert 'conflict' not in result
    payload = overlap_payload()
    payload['_server_messages'] = '[invalid json SECRET'
    assert 'SECRET' not in json.dumps(feedback(NativeHTTPFailure('POST', 417, payload), tool('hrms')))


def test_business_rejection_survives_agent_receipt_and_replay(tmp_path):
    from mac_bench.environments.tools import AppConnection, CallJournal, Role, WorldTools
    from mac_bench.environments.tools.contracts import Tool, object_schema
    from mac_bench.environments.tools.registry import ToolRegistry

    calls = []
    def reject(connection, args):
        calls.append(args)
        raise NativeHTTPFailure('POST', 417, overlap_payload())
    operation = Tool('hrms.shift_assignment.create', 'hrms', 'hrms.shift_assignment',
        'create', 'binding', {}, 'Create shift', object_schema(), 'Shift', False, reject)
    journal = CallJournal(tmp_path / 'calls.sqlite')
    runtime = WorldTools('instance', ToolRegistry([operation], catalog_revision='test'),
        [Role('operator', (AppConnection('hrms', {}, None),), ('*',))], journal)
    try:
        actor = runtime.agent('worker', 'operator')
        receipt = actor.call_tool(operation.name, {}, 'create-1')
        assert receipt['status'] == 'unknown'  # No unverified rollback guarantee.
        assert receipt['error']['conflict']['name'] == 'HR-SHA-26-11-00002'
        assert actor.call_tool(operation.name, {}, 'create-1')['replayed']
        assert len(calls) == 1
    finally:
        runtime.close()
        journal.close()
