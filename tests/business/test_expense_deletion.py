"""Expense deletion checks the native relation before any delete or event."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from mac_bench.environments.apps.bigcapital import accounting_patch as patches

FIXTURES = json.loads((Path(__file__).parent/'fixtures/expense_commands.json').read_text())

@pytest.mark.parametrize('name', FIXTURES)
def test_expense_patches_are_idempotent_and_require_pinned_source(name):
    fn = getattr(patches, 'patch_' + name)
    result = fn(FIXTURES[name]['source'])
    assert fn(result) == result
    with pytest.raises(ValueError):
        fn('unexpected build')


def test_expense_guard_precedes_writes_and_uses_the_deletion_transaction():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for native command tests')
    source = """const common_1={BadRequestException:class extends Error {
 constructor(x){super(x.errors[0].message);this.code=x.errors[0].type;}},Inject:()=>()=>{}};
const constants_1={ERRORS:{EXPENSE_HAS_ASSOCIATED_LANDED_COST:'EXPENSE_HAS_ASSOCIATED_LANDED_COST'}};
const events_1={events:{expenses:{onDeleting:'deleting',onDeleted:'deleted'}}};
const __decorate=(ds,target)=>target;
"""
    for name in FIXTURES:
        source += getattr(patches,'patch_'+name)(FIXTURES[name]['source'])+'\n'
    source += """(async()=>{
const out=[];
for(const associated of [false,true]){
 const events=[],trx={token:'outer'},guard=new CommandExpenseValidator();
 guard.billLandedCostModel=()=>({query:t=>{if(t!==trx)throw Error('wrong guard transaction');return {
 findOne:async where=>{if(JSON.stringify(where)!==JSON.stringify({fromTransactionType:'Expense',fromTransactionId:7}))throw Error('wrong relation scope');events.push('check');return associated?{id:42}:undefined;}};}});
 const chain={findById(){return this},forUpdate(){events.push('lock');return this;},withGraphFetched(){return this;},throwIfNotFound:async()=>({id:7,categories:[]}),delete:async()=>{events.push('delete-expense');}};
 const model=()=>({query:t=>{if(t!==trx)throw Error('wrong delete transaction');return chain;}});
 const cats=()=>({query:t=>{if(t!==trx)throw Error('wrong category transaction');return {where(){return this},delete:async()=>events.push('delete-categories')};}});
 const uow={withTransaction:async(fn,parent)=>{if(parent!==trx)throw Error('lost outer transaction');events.push('begin');try{const result=await fn(trx);events.push('commit');return result;}catch(e){events.push('rollback');throw e;}}};
 const service=new DeleteExpense({emitAsync:async name=>events.push(name)},uow,guard,model,cats);
 let error=null;try{await service.deleteExpense(7,trx);}catch(e){error=e.code||e.message;}
 out.push({associated,events,error});
}
console.log(JSON.stringify(out));})();"""
    result = subprocess.run([node,'-e',source],text=True,capture_output=True,check=True)
    allowed, blocked = json.loads(result.stdout)
    assert allowed['error'] is None
    assert allowed['events'] == ['begin','lock','check','deleting','delete-categories','delete-expense','deleted','commit']
    assert blocked['error'] == 'EXPENSE_HAS_ASSOCIATED_LANDED_COST'
    assert blocked['events'] == ['begin','lock','check','rollback']
