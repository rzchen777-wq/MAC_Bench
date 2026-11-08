"""Behavior of patched command bodies captured from the deployed native build.

The fixture contains class bodies only (no installed package or credentials).
Live API/readback evidence is recorded separately in the diagnostic directory.
"""
import json
from pathlib import Path
import subprocess

import pytest
from mac_bench.environments.apps.bigcapital import accounting_patch as patches
from mac_bench.environments.tools.adapters.rest import credit_refund_date_schema
from mac_bench.environments.tools.contracts import validate, ToolError

FIXTURES = json.loads((Path(__file__).parent/'fixtures/accounting_commands.json').read_text())
NODE = Path(__file__).resolve().parents[3]/'.cache/toolchain/node/bin/node'


def run(name, program):
    import shutil
    node = shutil.which('node') or (str(NODE) if NODE.exists() else None)
    if node is None:
        pytest.skip('Node is required for native command behavior')
    f = FIXTURES[name]
    source = getattr(patches, 'patch_' + name)(f['source'])
    script = '''const common_1={BadRequestException:class extends Error {constructor(x){super(JSON.stringify(x));}}};
const events_1={events:new Proxy({}, {get:()=>new Proxy({},{get:(_,k)=>k})})};
const ServiceError_1={ServiceError:Error};const constants_1={ERRORS:{}};
''' + source + '\nconst Native=' + f['class_name'] + ';\n' + program
    r = subprocess.run([node, '-e', script], text=True, capture_output=True, check=True)
    return json.loads(r.stdout)


@pytest.mark.parametrize('name', list(FIXTURES))
def test_pinned_patches_are_idempotent_and_reject_unrecognized_commands(name):
    fn = getattr(patches,'patch_'+name)
    patched = fn(FIXTURES[name]['source'])
    assert fn(patched) == patched
    with pytest.raises(ValueError):
        fn('different implementation')


@pytest.mark.parametrize('name,old,method,dto,model,validator,transform', [
    ('invoice_edit',{'paymentAmount':90,'creditedAmount':30,'writtenoffAmount':5,'customerId':201},'editSaleInvoice','saleInvoiceDTO','saleInvoiceModel','validators','tranformEditDTOToModel'),
    ('bill_edit',{'paymentAmount':90,'creditedAmount':30,'vendorId':201,'entries':[]},'editBill','billDTO','billModel','validators','transformerDTO'),
    ('vendor_edit',{'refundedAmount':30,'invoicedAmount':90,'vendorId':201},'editVendorCredit','vendorCreditDTO','vendorCreditModel',None,'vendorCreditDTOTransform'),
])
def test_edits_preserve_settlements_and_party(name,old,method,dto,model,validator,transform):
    program = '''(async()=>{
const output=[];
for (const [amount,party] of [[100,201],[150,202],[150,201]]) {
 const x=new Native(),events=[],old=OLD;
 const trx={}; x.uow={withTransaction:async(fn)=>{events.push('begin');try{const r=await fn(trx);events.push('commit');return r;}catch(e){events.push('rollback');throw e;}}};
 const chain={findById(){return this;},forUpdate(){events.push('lock');return this;},
 withGraphJoined:async()=>old,withGraphFetched:async()=>old,throwIfNotFound:async()=>old,
 upsertGraphAndFetch:async obj=>{events.push('write');return obj;}};
 x.MODEL=()=>({query:t=>{if(t!==trx)throw Error('outside transaction');return chain;}});
 const partyModel=()=>({query:()=>({modify(){return this;},findById(){return this;},throwIfNotFound:async()=>({currencyCode:'USD'})})});
 x.customerModel=x.vendorModel=x.contactModel=partyModel;
 x.eventPublisher={emitAsync:async()=>{}};
 x.itemsEntriesService=new Proxy({}, {get:()=>async()=>{}});
 x.validators=new Proxy({}, {get:()=>async()=>{}});
 x.transactionLandedCostEntries=new Proxy({}, {get:()=>async()=>{}});
 x.tranformEditDTOToModel=async()=>({balance:amount});
 x.transformerDTO={billDTOToModel:async()=>({amount,entries:[]})};
 x.vendorCreditDTOTransform={transformCreateEditDTOToModel:async()=>({amount})};
 let error=null;try{await x.METHOD(1,{customerId:party,vendorId:party,entries:[]});}catch(e){error=e.message;}
 output.push({amount,party,error,events});
}
console.log(JSON.stringify(output));})();'''.replace('OLD',json.dumps(old)).replace('MODEL',model).replace('METHOD',method)
    rows=run(name,program)
    for r in rows[:2]:
        assert r['error'] and 'SETTLED' in r['error'] or r['error'] and ('USED' in r['error'] or 'SETTLEMENTS' in r['error'])
        assert r['events']==['begin','lock','rollback']
    assert rows[2]['error'] is None
    assert rows[2]['events']==['begin','lock','write','commit']


def test_vendor_allocation_checks_each_bill_instead_of_batch_total():
    r=run('vendor_allocation','''
const VendorCreditsApplyBills_constants_1={ERRORS:{BILLS_HAS_NO_REMAINING_AMOUNT:'no balance'}};
const x=new Native(),bills=[{id:1,dueAmount:30},{id:2,dueAmount:60}],results=[];
for (const entries of [[{billId:1,amount:30},{billId:2,amount:60}], [{billId:1,amount:31}], [{billId:1,amount:20},{billId:1,amount:20}]]) {
 try{x.validateBillsRemainingAmount(bills,entries);results.push(true);}catch(e){results.push(false);}
}console.log(JSON.stringify(results));''')
    assert r == [True,False,False]


def test_refund_date_rejects_timestamp_before_native_write():
    schema={'type':'object','properties':{'date':{'type':'string','format':'date-time'}},'required':['date']}
    credit_refund_date_schema(schema)
    validate(schema,{'date':'2026-10-12'})
    with pytest.raises(ToolError):validate(schema,{'date':'2026-10-12T00:00:00Z'})


def test_vendor_refund_commits_record_and_single_credit_balance_atomically():
    result=run('vendor_refund_create','''(async()=>{
const results=[];
for(const failEvent of [false,true]) {
 let credits={1:{id:1,refundedAmount:0,creditsRemaining:120},2:{id:2,refundedAmount:0}},refunds=[];
 const x=new Native(),trx={};
 x.uow={withTransaction:async fn=>{const saved=JSON.stringify({credits,refunds});try{return await fn(trx);}catch(e){({credits,refunds}=JSON.parse(saved));throw e;}}};
 x.vendorCreditModel=()=>({query:t=>{if(t!==trx)throw Error('balance outside transaction');return {
  findById:id=>({forUpdate(){return this;},throwIfNotFound:async()=>credits[id],increment:async(field,n)=>credits[id][field]+=n})};}});
 x.accountModel=()=>({query:()=>({findById:()=>({throwIfNotFound:async()=>({accountType:'bank'})})})});
 x.refundVendorCreditModel=()=>({query:t=>{if(t!==trx)throw Error('refund outside transaction');return {insertAndFetch:async obj=>{refunds.push(obj);return obj;}};}});
 x.transformDTOToModel=async(c,d)=>({...d,vendorCreditId:c.id});
 x.eventPublisher={emitAsync:async event=>{if(failEvent&&event==='onRefundCreated')throw Error('posting failed');}};
 let error=null;try{await x.createRefund(1,{amount:30,depositAccountId:9});}catch(e){error=e.message;}
 results.push({credits,refunds,error});
}console.log(JSON.stringify(results));})();''')
    assert result[0]['error'] is None
    assert result[0]['credits']['1']['refundedAmount']==30
    assert result[0]['credits']['2']['refundedAmount']==0
    assert len(result[0]['refunds'])==1
    assert result[1]['error']=='posting failed'
    assert result[1]['credits']['1']['refundedAmount']==0
    assert result[1]['refunds']==[]


def test_vendor_refund_reversal_rolls_back_if_posting_fails():
    result=run('vendor_refund_delete','''(async()=>{
const results=[];
for(const failEvent of [false,true]) {
 let balances={1:30,2:7},exists=true;
 const x=new Native(),trx={};
 const refund={amount:30,$relatedQuery:(name,t)=>{
  if(name!=='vendorCredit'||t!==trx)throw Error('wrong relation transaction');
  return {forUpdate(){return this;},first:async()=>({id:1}),decrement:async(field,n)=>balances[1]-=n};}};
 x.refundVendorCreditModel=()=>({query:t=>{if(t!==trx)throw Error('outside transaction');return {
  findById:()=>({forUpdate(){return this;},throwIfNotFound:async()=>refund,delete:async()=>{exists=false;}})};}});
 x.uow={withTransaction:async fn=>{try{return await fn(trx);}catch(e){balances={1:30,2:7};exists=true;throw e;}}};
 x.eventPublisher={emitAsync:async event=>{if(failEvent&&event==='onRefundDeleted')throw Error('posting failed');}};
 let error=null;try{await x.deleteRefundVendorCreditRefund(8);}catch(e){error=e.message;}
 results.push({balances,exists,error});
}console.log(JSON.stringify(results));})();''')
    assert result[0]=={'balances':{'1':0,'2':7},'exists':False,'error':None}
    assert result[1]=={'balances':{'1':30,'2':7},'exists':True,'error':'posting failed'}


def test_native_allocation_rejects_invalid_entries_before_touching_models():
    result=run('vendor_allocation','''(async()=>{
const results=[];
for (const entries of [null,[],[{billId:1,amount:-5}],[{billId:1,amount:0}],
 [{billId:1,amount:1},{billId:1,amount:2}],[{billId:1,amount:'5'}],[{billId:0,amount:5}]]) {
 const x=new Native();let touched=false;
 x.vendorCreditModel=()=>{touched=true;throw Error('model read');};
 let error=null;try{await x.applyVendorCreditToBills(1,{entries});}catch(e){error=e.message;}
 results.push({touched,error});
}console.log(JSON.stringify(results));})();''')
    assert len(result)==7
    for r in result:
        assert not r['touched']
        assert 'INVALID_VENDOR_CREDIT_ALLOCATION' in r['error']


def test_tool_contract_rejects_nonpositive_vendor_allocations():
    from types import SimpleNamespace
    from mac_bench.environments.tools.adapters.rest import bigcapital_operation
    name='applyVendorCreditToBills'
    binding={'entity':'bigcapital.vendor_credit_applied_bill','operation_bindings':{name:[
        {'method':'POST','path_template':'/api/vendor-credits/:vendorCreditId/apply-to-bills'}]}}
    body={'type':'object','properties':{'entries':{'type':'array','items':{'type':'object',
        'properties':{'billId':{'type':'integer'},'amount':{'type':'number'}},'required':['billId','amount']}}},'required':['entries']}
    operation={'input':{'library':'native','parameters':[
        {'in':'path','name':'vendorCreditId','schema':{'type':'integer'}},
        {'in':'body','schema':body}]}}
    schema,*_=bigcapital_operation(SimpleNamespace(types=lambda _:{}),binding,name,operation)
    validate(schema,{'path':{'vendorCreditId':1},'body':{'entries':[{'billId':1,'amount':5}]}})
    for entries in [[],[{'billId':1,'amount':-5}],[{'billId':1,'amount':0}],[{'billId':0,'amount':5}]]:
        with pytest.raises(ToolError):
            validate(schema,{'path':{'vendorCreditId':1},'body':{'entries':entries}})
