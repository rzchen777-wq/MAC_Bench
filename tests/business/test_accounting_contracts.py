"""The public accounting contract and receipts must describe the same effects."""
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac_bench.environments.catalog.entities import EntityCatalog
from mac_bench.environments.errors import NativeHTTPFailure
from mac_bench.environments.tools import AppConnection, CallJournal, Role, WorldTools, build_registry
from mac_bench.environments.tools.adapters.accounting import NO_WRITE_ERRORS
from mac_bench.environments.tools.contracts import NativeResult, ToolError, validate
from mac_bench.environments.tools.registry import ToolRegistry


@pytest.fixture(scope='module')
def registry():
    import os
    from mac_bench.runtime.paths import repository_root
    task_root = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root()/'data/tasks'))
    path = task_root/'op26_business_finance_008/environment/runtime/catalog/entity/resources/catalog.json.gz'
    if not path.is_file():
        pytest.skip('Install task data to check native application contracts')
    catalog = EntityCatalog.load(path)
    entities = {entity for entity, _ in NO_WRITE_ERRORS}
    bindings = [b for e in entities for v in catalog.variants(e) for b in v['bindings']]
    return build_registry(catalog, bindings=bindings)


@pytest.mark.parametrize('entity,operation', NO_WRITE_ERRORS)
def test_reviewed_rejections_survive_public_receipt_and_replay(registry, tmp_path, entity, operation):
    tool = next(t for t in registry.tools() if (t.entity, t.operation) == (entity, operation))
    # Use the real adapter/transport path, replacing only the native HTTP server.
    seen = []
    response = SimpleNamespace(status=400, body=None)
    def request(*args, **kwargs):
        seen.append(args)
        raise NativeHTTPFailure(args[0], response.status, response.body)
    connection = AppConnection('bigcapital', tool.scope, SimpleNamespace(request_response=request))
    journal = CallJournal(tmp_path/'calls.sqlite')
    runtime = WorldTools('test', ToolRegistry([tool], catalog_revision='test'),
                         [Role('operator', (connection,), ('*',))], journal)
    arguments = tool.describe()['call_example']['arguments']
    try:
        agent = runtime.agent('worker', 'operator')
        for index, kind in enumerate(sorted(NO_WRITE_ERRORS[entity, operation])):
            response.body = {'errors': [{'type': kind, 'stack': 'SECRET'}]}
            receipt = agent.call_tool(tool.name, arguments, str(index))
            assert receipt['status'] == 'rejected'
            assert receipt['error']['native_error_type'] == kind
            assert 'No changes were made' in receipt['error']['message']
            assert 'SECRET' not in json.dumps(receipt)
            count = len(seen)
            assert agent.call_tool(tool.name, arguments, str(index))['replayed']
            assert len(seen) == count
    finally:
        runtime.close()
        journal.close()


@pytest.mark.parametrize('status,errors', [
    (500, [{'type': 'INVOICES_HAS_NO_REMAINING_AMOUNT'}]),
    (400, [{'type': 'INVOICES_HAS_NO_REMAINING_AMOUNT'}, {'type': 'UNREVIEWED'}]),
    (400, [{'type': 'BILLS_HAS_NO_REMAINING_AMOUNT'}]),
    (400, []),
])
def test_unreviewed_failure_does_not_claim_no_write(registry, status, errors):
    tool = registry.get('bigcapital.credit_note_applied_invoice.applyCreditNoteToInvoices')
    def request(*args, **kwargs):
        raise NativeHTTPFailure(args[0], status, {'errors': errors})
    transport = SimpleNamespace(request_response=request)
    with pytest.raises(NativeHTTPFailure) as raised:
        tool.handler(AppConnection('bigcapital', tool.scope, transport), tool.describe()['call_example']['arguments'])
    assert raised.value.outcome == 'unknown'


@pytest.mark.parametrize('entity,operation,document_id', [
    ('credit_note_applied_invoice', 'applyCreditNoteToInvoices', 'invoiceId'),
    ('vendor_credit_applied_bill', 'applyVendorCreditToBills', 'billId'),
])
def test_allocation_amount_and_duplicate_contract(registry, entity, operation, document_id):
    tool = registry.get(f'bigcapital.{entity}.{operation}')
    card = tool.describe()
    amount = card['request']['schema']['properties']['body']['properties']['entries']['items']['properties']['amount']
    assert 'Additional credit' in amount['description'] and 'Not a cumulative target' in amount['description']
    args = card['call_example']['arguments']
    args['body']['entries'] = [{document_id: 1, 'amount': 10}, {document_id: 1, 'amount': 20}]
    with pytest.raises(ToolError, match='one entry'):
        tool.handler(None, args)  # Rejected before touching a native connection.
    args['body']['entries'] = [{document_id: 1, 'amount': 0}]
    with pytest.raises(ToolError):
        validate(tool.input_schema, args)


def test_document_amounts_are_not_conflated(registry):
    invoice = registry.get('bigcapital.sale_invoice.getSaleInvoice')
    bill = registry.get('bigcapital.bill.getBill')
    assert 'before settlement' in invoice.native_contract['output']['amount_fields']['balance']
    assert 'Settled amount' in bill.native_contract['output']['amount_fields']['balance']
    assert 'writtenoff_amount' in invoice.native_contract['output']['amount_fields']['balance_amount']
    assert 'due_amount' in invoice.describe()['returns']
    assert 'credits_remaining' in registry.get('bigcapital.credit_note.getCreditNote').describe()['returns']


@pytest.mark.parametrize('name', ['bigcapital.sale_invoice.getSaleInvoice', 'bigcapital.bill.getBill'])
def test_partial_settlement_is_not_presented_as_full_cash_payment(registry, name):
    tool = registry.get(name)
    # Both the short usage card and detailed contract must explain the native flag.
    assert 'is_fully_paid means due_amount is zero' in tool.describe()['returns']
    states = tool.native_contract['output']['state_fields']
    assert 'partially or fully settled' in states['is_paid']
    assert 'due_amount is zero' in states['is_fully_paid']
    assert 'credits' in states['is_partially_paid']


@pytest.mark.parametrize('entity,operation,amounts,party', [
    ('sale_invoice', 'getSaleInvoice', {'total':1500, 'payment_amount':50, 'credited_amount':500,
      'writtenoff_amount':None, 'due_amount':950, 'is_paid':True, 'is_fully_paid':False}, 'customer'),
    ('bill', 'getBill', {'total':1500, 'payment_amount':50, 'credited_amount':500,
      'due_amount':950, 'balance':550, 'is_paid':True, 'is_fully_paid':False}, 'vendor'),
    ('credit_note', 'getCreditNote', {'amount':1200, 'invoices_amount':500,
      'refunded_amount':None, 'credits_used':500, 'credits_remaining':700}, 'customer'),
    ('vendor_credit', 'getVendorCredit', {'amount':1200, 'invoiced_amount':500,
      'refunded_amount':None, 'credits_remaining':700}, 'vendor'),
])
def test_document_overview_keeps_amounts_and_full_data_recoverable(
        registry, tmp_path, monkeypatch, entity, operation, amounts, party):
    from mac_bench.evaluation.tasks import public_tool_client
    from mac_bench.environments.tools.api import dispatch
    raw = {'id':21, 'currency_code':'USD', party+'_id':7, **amounts,
           party:{'id':7, 'display_name':'Example account', 'currency_code':'USD',
                  'billing_address1':'Original address'},
           'entries':[{'item_id':4,'description':'A long unchanged description '*100,
                       'quantity':1,'rate':1200}],
           'custom_field':'Preserve this exact value'}
    calls=[]
    tool=registry.get(f'bigcapital.{entity}.{operation}')
    tool=replace(tool,handler=lambda c,a:(calls.append(a),NativeResult(deepcopy(raw)))[1])
    journal=CallJournal(tmp_path/'calls.sqlite')
    world=WorldTools('world',ToolRegistry([tool],catalog_revision='test'),
        [Role('operator',(AppConnection('bigcapital',tool.scope,None),),('*',))],journal)
    try:
        agent=world.agent('a','operator')
        args=tool.describe()['call_example']['arguments']
        receipt=agent.call_tool(tool.name,args,'first')
        assert receipt['data_view']=='document_overview' and not receipt['data_complete']
        assert receipt['data']['currency_code']=='USD'
        assert receipt['data'][party]=={'id':7,'display_name':'Example account','currency_code':'USD'}
        for k,v in amounts.items():
            if k not in ('is_paid','balance'):assert receipt['data'][k]==v
        assert 'is_paid' not in receipt['data'] and 'entries' not in receipt['data']
        assert receipt['data'].get('refunded_amount') is None  # no fabricated zero
        monkeypatch.setattr(public_tool_client,'call',lambda name,**kwargs:dispatch(agent,name,kwargs))
        observation=public_tool_client.load_result(receipt['result_ref'])
        assert observation['data']==raw
        assert observation['observed_at']==receipt['observed_at']
        assert public_tool_client.load_result(receipt['result_ref'],'/data/entries')['data']==raw['entries']
        projected=agent.query_result(receipt['result_ref'],keys=['custom_field'])
        assert projected['total_matched']==1
        replay=agent.call_tool(tool.name,args,'first')
        assert replay['replayed'] and replay['data']==receipt['data']
        assert replay['observed_at']==receipt['observed_at'] and len(calls)==1
        with pytest.raises(ToolError):world.agent('b','operator').read_result(receipt['result_ref'])
        raw['custom_field']='Changed live record'
        assert public_tool_client.load_result(receipt['result_ref'])['data']['custom_field']=='Preserve this exact value'
    finally:
        world.close();journal.close()


def test_overview_rejects_invalid_detail_shape_and_leaves_other_operations_alone(registry):
    from mac_bench.environments.tools.presentation import record_overview
    tool=registry.get('bigcapital.sale_invoice.getSaleInvoice')
    for invalid in ({'id':1}, [{'id':1,'total':20,'currency_code':'USD'}],
                    {'id':1, 'total':'20', 'currency_code':'USD'}):
        with pytest.raises(ToolError, match='does not match'):
            record_overview(tool, invalid)
    create=registry.get('bigcapital.sale_invoice.editSaleInvoice')
    assert record_overview(create,{'id':1,'total':20,'currency_code':'USD','entries':[]}) is None


def test_document_examples_and_projection_follow_public_response_schema(registry):
    from jsonschema import Draft202012Validator
    from mac_bench.environments.tools.adapters.accounting import DOCUMENTS
    from mac_bench.environments.tools.presentation import record_overview
    for entity, (operation, _, party, _) in DOCUMENTS.items():
        tool = registry.get(f'{entity}.{operation}')
        schema = tool.native_contract['output']['record_overview']
        Draft202012Validator.check_schema(schema)
        card = tool.describe()
        sample = card['response_example']['data']
        Draft202012Validator(schema).validate(sample)
        assert card['output_navigation']['path'] == '/record_overview'
        # Nested nulls and omitted optional fields remain distinguishable.
        raw = {**sample, party: None, 'entries': [{'quantity': 1, 'rate': 1200}]}
        del raw['reference_no']
        view = record_overview(tool, raw)
        assert view[party] is None and 'reference_no' not in view
        assert 'entries' not in view and raw['entries'][0]['rate'] == 1200
        Draft202012Validator(schema).validate(view)


def test_unrelated_outputs_do_not_advertise_document_amounts(registry):
    from mac_bench.environments.tools.adapters.accounting import DOCUMENTS, DOCUMENT_COLLECTIONS
    for tool in registry.tools():
        if tool.entity not in DOCUMENTS:
            continue
        detail = DOCUMENTS[tool.entity][0]
        if tool.operation in {detail, *DOCUMENT_COLLECTIONS[tool.entity]}:
            continue
        output = tool.native_contract['output']
        assert not {'record_overview', 'amount_fields', 'state_fields'} & output.keys()
        assert output['document_read']['tool_name'] == f'{tool.entity}.{detail}'
        assert 'is_paid' not in tool.describe()['returns']


@pytest.mark.parametrize('padding', [0, 16000])
def test_invalid_view_preserves_native_success_and_recoverable_evidence(registry, tmp_path, padding):
    original = {'id': 4, 'total': 'unexpected', 'currency_code': 'USD', 'native_extra': 'x'*padding}
    tool = replace(registry.get('bigcapital.sale_invoice.getSaleInvoice'),
                   handler=lambda c, a: NativeResult(deepcopy(original)))
    journal = CallJournal(tmp_path/'calls.sqlite')
    world = WorldTools('world', ToolRegistry([tool], catalog_revision='test'),
        [Role('operator', (AppConnection('bigcapital', tool.scope, None),), ('*',))], journal)
    try:
        agent = world.agent('a', 'operator')
        receipt = agent.call_tool(tool.name, tool.describe()['call_example']['arguments'], 'read')
        assert receipt['status'] == 'succeeded'
        assert receipt['presentation_error']['code'] == 'response_schema_mismatch'
        assert 'data_view' not in receipt
        if not padding:
            assert receipt['data'] == original and receipt['data_complete']
        saved, _ = journal.receipt('world', 'a', 'operator', receipt['result_ref'])
        assert saved['data'] == original
        replay = agent.call_tool(tool.name, tool.describe()['call_example']['arguments'], 'read')
        assert replay['presentation_error'] == receipt['presentation_error']
    finally:
        world.close()
        journal.close()


def test_native_vendor_example_uses_the_actual_item_schema(registry):
    tool = registry.get('bigcapital.vendor_credit.editVendorCredit')
    card = tool.describe()
    arguments = card['call_example']['arguments']
    validate(tool.input_schema, arguments)
    assert arguments['body']['entries'][0]['rate'] == 1
    assert 'unitPrice' not in json.dumps(card['request']['schema'])


def test_example_review_resolves_root_references_and_preserves_valid_values():
    from mac_bench.environments.tools.examples import review_examples, example
    schema = {'type': 'object', '$defs': {'row': {'type': 'object',
        'properties': {'rate': {'type': 'number', 'minimum': 1, 'examples': [2]}},
        'required': ['rate'], 'additionalProperties': False}},
        'properties': {'entries': {'type': 'array', 'items': {'$ref': '#/$defs/row'},
            'examples': [[{'unitPrice': 1}], [{'rate': 2}]]}}, 'required': ['entries']}
    review_examples(schema)
    assert schema['properties']['entries']['examples'] == [[{'rate': 2}]]
    assert example(schema)[0]['arguments'] == {'entries': [{'rate': 2}]}
