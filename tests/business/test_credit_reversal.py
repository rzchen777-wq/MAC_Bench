"""Customer allocation reversal is discoverable, precise, and transactional."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from mac_bench.environments.apps.bigcapital.accounting_patch import (
    patch_credit_unapply_controller, patch_credit_unapply_transaction,
    patch_vendor_unapply_transaction,
)
from mac_bench.environments.catalog.entities import EntityCatalog
from mac_bench.environments.tools import build_registry
from mac_bench.environments.tools.contracts import ToolError, validate

FIXTURE = json.loads((Path(__file__).parent/'fixtures/credit_reversal.json').read_text())


@pytest.fixture(scope='module')
def tool():
    import os
    from mac_bench.runtime.paths import repository_root
    task_root = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root()/'data/tasks'))
    path = task_root/'op26_business_finance_008/environment/runtime/catalog/entity/resources/catalog.json.gz'
    if not path.is_file():
        pytest.skip('Install task data to check native application contracts')
    catalog = EntityCatalog.load(path)
    binding = catalog.variants('bigcapital.credit_note_applied_invoice')[0]['bindings'][0]
    registry = build_registry(catalog, bindings=[binding])
    result = registry.get('bigcapital.credit_note_applied_invoice.deleteAppliedInvoiceToCreditNote')
    assert any(t['name'] == result.name for t in registry.list_tools(query='unapply')['items'])
    description = registry.describe_tool(result.name)
    assert description['call_example']['arguments'] == {'path': {'creditNoteAppliedInvoiceId': 1}}
    return result


def test_delete_uses_allocation_id_and_bound_transport(tool):
    calls = []
    def request(method, path, body, *, rate_limit_key=None):
        calls.append((method, path, body))
        return SimpleNamespace(status=200, body={'deleted': True, 'credit_note_applied_invoice_id': 9})
    args = {'path': {'creditNoteAppliedInvoiceId': 9}}
    validate(tool.input_schema, args)
    result = tool.handler(SimpleNamespace(transport=SimpleNamespace(request_response=request)), args)
    assert calls == [('DELETE', 'api/credit-notes/applied-invoices/9', None)]
    assert result.data['deleted'] and not tool.read_only


@pytest.mark.parametrize('path', [{'id': 9}, {'creditNoteId': 9}, {'invoiceId': 9},
    {'creditNoteAppliedInvoiceId': -1}, {'creditNoteAppliedInvoiceId': 1.2}])
def test_wrong_locator_rejected(tool, path):
    with pytest.raises(ToolError):
        validate(tool.input_schema, {'path': path})


def test_controller_wiring_and_transaction_behavior(tmp_path):
    node = shutil.which('node') or str(Path('.cache/toolchain/node/bin/node').resolve())
    controller = patch_credit_unapply_controller(FIXTURE['controller'])
    command = patch_credit_unapply_transaction(FIXTURE['command'])
    assert patch_credit_unapply_controller(controller) == controller
    assert patch_credit_unapply_transaction(command) == command
    for source in [controller, command]:
        f = tmp_path/'check.js'; f.write_text(source)
        subprocess.run([node, '--check', str(f)], check=True, capture_output=True)
    # Execute the actual command class with transactional model doubles.
    body = command[command.index('let DeleteCreditNoteApplyToInvoices = class'):command.index('exports.DeleteCreditNoteApplyToInvoices = DeleteCreditNoteApplyToInvoices;')]
    program = '''const ServiceError_1={ServiceError:Error};const constants_1={ERRORS:{CREDIT_NOTE_APPLY_TO_INVOICES_NOT_FOUND:'not found'}};
const events_1={events:{creditNote:{onApplyToInvoicesDeleted:'deleted'}}};
''' + body + '''
(async()=>{
 let exists=true; const log=[],trx={};
 const row={id:7,invoiceId:11,creditNoteId:3,amount:50};
 const model=()=>({query(t){if(t!==trx)throw Error('outside transaction');return {
  findById(){return this},forUpdate:async()=>{log.push('lock');return exists?row:null},
  delete:async()=>{exists=false;log.push('delete')}}}});
 const credit=()=>({query(t){if(t!==trx)throw Error('outside transaction');return {findById(){return this},throwIfNotFound:async()=>({id:3})}}});
 const x=new DeleteCreditNoteApplyToInvoices({withTransaction:async f=>f(trx)}, {emitAsync:async()=>{}}, model,credit,
  {decrementInvoiceCreditedAmount:async(id,amount,t)=>log.push(['invoice',id,amount,t===trx])},
  {decrementCreditNoteInvoicedAmount:async(id,amount,t)=>log.push(['credit',id,amount,t===trx])});
 await x.deleteApplyCreditNoteToInvoices(7);
 let rejected=false;try{await x.deleteApplyCreditNoteToInvoices(7)}catch(e){rejected=true}
 console.log(JSON.stringify({log,rejected}));
})();'''
    r = subprocess.run([node, '-e', program], text=True, capture_output=True, check=True)
    assert json.loads(r.stdout) == {'rejected': True, 'log': ['lock', 'delete', ['invoice',11,50,True], ['credit',3,50,True], 'lock']}


@pytest.mark.parametrize('vendor', [False, True])
def test_two_concurrent_reversals_decrement_only_once(vendor):
    node = shutil.which('node') or str(Path('.cache/toolchain/node/bin/node').resolve())
    original = FIXTURE['vendor_command' if vendor else 'command']
    patch = patch_vendor_unapply_transaction if vendor else patch_credit_unapply_transaction
    name = 'DeleteApplyVendorCreditToBillService' if vendor else 'DeleteCreditNoteApplyToInvoices'
    method = 'deleteApplyVendorCreditToBills' if vendor else 'deleteApplyCreditNoteToInvoices'
    for corrected in (False, True):
        source = patch(original) if corrected else original
        if corrected:
            assert patch(source) == source
        body = source[source.index('let '+name+' = class'):source.index('exports.'+name+' = '+name+';')]
        program = '''const ServiceError_1={ServiceError:Error};
const constants_1={ERRORS:{CREDIT_NOTE_APPLY_TO_INVOICES_NOT_FOUND:'not found'}};
const VendorCreditsApplyBills_constants_1={ERRORS:{VENDOR_CREDIT_APPLY_TO_BILLS_NOT_FOUND:'not found'}};
const events_1={events:{creditNote:{onApplyToInvoicesDeleted:'deleted'},vendorCredit:{onApplyToInvoicesDeleted:'deleted'}}};
''' + body + '''
(async()=>{
 let exists=true,tail=Promise.resolve(),decrements=0;
 const row={id:7,invoiceId:11,billId:11,creditNoteId:3,vendorCreditId:3,amount:50};
 const model=()=>({query(){return {
  findById(){return this},then(resolve){resolve(exists?row:null)},
  forUpdate:async()=>exists?row:null,delete:async()=>{exists=false}}}});
 const credit=()=>({query(){return {findById(){return this},throwIfNotFound:async()=>({id:3})}}});
 const uow={withTransaction(fn){const p=tail.then(()=>fn({}));tail=p.catch(()=>{});return p}};
 const x=new NATIVE(uow,{emitAsync:async()=>{}},model,credit,
  {decrementInvoiceCreditedAmount:async()=>{decrements++},decrementBillCreditedAmount:async()=>{decrements++}},
  {decrementCreditNoteInvoicedAmount:async()=>{},decrementVendorCreditInvoicedAmount:async()=>{}});
 const results=await Promise.allSettled([x.METHOD(7),x.METHOD(7)]);
 console.log(JSON.stringify({decrements,fulfilled:results.filter(r=>r.status==='fulfilled').length}));
})();'''.replace('NATIVE',name).replace('METHOD',method)
        r = subprocess.run([node, '-e', program], text=True, capture_output=True, check=True)
        expected = 1 if corrected else 2
        assert json.loads(r.stdout) == {'decrements': expected, 'fulfilled': expected}
