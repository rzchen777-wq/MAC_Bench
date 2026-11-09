"""Regression checks for the pinned native credit-note editing correction."""
import json
import shutil
import subprocess

import pytest

from mac_bench.environments.apps.bigcapital.accounting_patch import (
    patch_application, patch_edit, patch_transform,
)


@pytest.fixture
def node():
    executable = shutil.which('node')
    if executable is None:
        pytest.skip('Node is needed to exercise native JavaScript')
    return executable


def execute(node, source):
    result = subprocess.run([node, '-e', source], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_edit_does_not_reset_allocations_or_refunds(node):
    source = '''function transform(oldCreditNote) { return {
                refundedAmount: 0,
                invoicesAmount: 0,
    }; }'''
    patched = patch_transform(source)
    assert patch_transform(patched) == patched
    result = execute(node, patched + '''
const existing={amount:120,invoicesAmount:30,refundedAmount:10,openedAt:'2026-10-12'};
console.log(JSON.stringify({created:transform(),edited:{...existing,...transform(existing)}}));
''')
    assert result['created'] == {'refundedAmount': 0, 'invoicesAmount': 0}
    assert result['edited']['invoicesAmount'] == 30
    assert result['edited']['refundedAmount'] == 10
    assert result['edited']['openedAt'] == '2026-10-12'


def test_edit_returns_committed_document_and_does_not_read_after_failed_edit(node):
    source = '''class Application {
    editCreditNote(creditNoteId, creditNoteDTO) {
        return this.editCreditNoteService.editCreditNote(creditNoteId, creditNoteDTO);
    }
}'''
    patched = patch_application(source)
    assert patch_application(patched) == patched
    result = execute(node, patched + '''
(async()=>{
 const app=new Application(),events=[];
 app.editCreditNoteService={editCreditNote:async()=>{events.push('commit');return {id:'21'};}};
 app.getCreditNoteService={getCreditNote:async()=>{events.push('read');return {id:21,credits_used:30,is_open:true};}};
 const value=await app.editCreditNote(21,{});
 app.editCreditNoteService.editCreditNote=async()=>{events.push('rejected');throw Error('invalid');};
 try {await app.editCreditNote(21,{});} catch(e) {}
 console.log(JSON.stringify({value,events}));
})();''')
    assert result['events'] == ['commit', 'read', 'rejected']
    assert result['value'] == {'id': 21, 'credits_used': 30, 'is_open': True}


@pytest.mark.parametrize('patch', [patch_transform, patch_edit, patch_application])
def test_unrecognized_native_source_is_not_silently_modified(patch):
    with pytest.raises(ValueError):
        patch('different native implementation')


def test_edit_checks_committed_credit_usage_before_mutation(node):
    # The native method's query/transaction shape; its model is substituted with
    # an in-memory ledger to exercise the injected preconditions and lock order.
    source = '''class Editor {
    async editCreditNote(creditNoteId, creditNoteEditDTO) {
        const oldCreditNote = await this.creditNoteModel()
            .query()
            .findById(creditNoteId)
            .throwIfNotFound();
        const creditNoteModel = creditNoteEditDTO;
        return this.uow.withTransaction(async (trx) => {
            const creditNote = await this.creditNoteModel().query(trx).upsertGraph(creditNoteModel);
            return creditNote;
        });
    }
}'''
    patched = patch_edit(source)
    assert patch_edit(patched) == patched
    result = execute(node, 'const common_1={BadRequestException:class extends Error{}};\n' + patched + '''
(async()=>{
 const results=[];
 for(const [amount,customerId] of [[39,201],[40,202],[40,201],[150,201]]){
  const editor=new Editor(),events=[],trx={};
  editor.uow={withTransaction:async fn=>{events.push('begin');try {
    const v=await fn(trx);events.push('commit');return v;
   }catch(e){events.push('rollback');throw e;}}};
  editor.creditNoteModel=()=>({query:t=>{
    if(t!==trx)throw Error('read outside transaction');
    return {findById:()=>({forUpdate:()=>{events.push('lock');return {
      throwIfNotFound:async()=>({amount:120,creditsUsed:40,customerId:201})};}}),
      upsertGraph:async value=>{events.push('write');return value;}};
  }});
  let ok=true;try{await editor.editCreditNote(21,{amount,customerId});}catch(e){ok=false;}
  results.push({amount,customerId,ok,events});
 }
 console.log(JSON.stringify(results));
})();''')
    for row in result[:2]:
        assert not row['ok']
        assert row['events'] == ['begin', 'lock', 'rollback']
    for row in result[2:]:
        assert row['ok']
        assert row['events'] == ['begin', 'lock', 'write', 'commit']
