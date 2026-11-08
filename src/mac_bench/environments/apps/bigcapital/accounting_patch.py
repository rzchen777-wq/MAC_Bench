"""Correct settlement invariants in the pinned native BigCapital build.

Installed before application startup on load and reset. All writes remain in
native tenant transactions; no task data or verifier rules are changed.
"""
import inspect


def replace_once(source, old, new):
    if new and new in source:
        return source
    if source.count(old) != 1:
        raise ValueError('Native accounting command changed: ' + old[:100])
    return source.replace(old, new)


def patch_credit_unapply_controller(source):
    """Expose the installed customer allocation reversal command through HTTP."""
    if '// MAC-Bench: customer allocation reversal endpoint.' in source:
        return source
    source = replace_once(source, 'const common_1 = require("@nestjs/common");',
        'const common_1 = require("@nestjs/common");\nconst CreditUnapply = require("./commands/DeleteCreditNoteApplyToInvoices.service").DeleteCreditNoteApplyToInvoices;')
    source = replace_once(source,
        'constructor(getCreditNoteAssociatedAppliedInvoicesService, creditNoteApplyToInvoicesService) {',
        'constructor(getCreditNoteAssociatedAppliedInvoicesService, creditNoteApplyToInvoicesService, creditUnapply) {\n        this.creditUnapply = creditUnapply;')
    source = replace_once(source, '    appliedCreditNoteToInvoices(creditNoteId) {', '''    // MAC-Bench: customer allocation reversal endpoint.
    async deleteAppliedInvoiceToCreditNote(creditNoteAppliedInvoiceId) {
        const id = Number(creditNoteAppliedInvoiceId);
        if (!Number.isSafeInteger(id) || id <= 0) {
            throw new common_1.BadRequestException('creditNoteAppliedInvoiceId must be a positive integer');
        }
        await this.creditUnapply.deleteApplyCreditNoteToInvoices(id);
        return { deleted: true, creditNoteAppliedInvoiceId: id };
    }
    appliedCreditNoteToInvoices(creditNoteId) {''')
    source = replace_once(source,
        'CreditNoteApplyToInvoices_service_1.CreditNoteApplyToInvoices])',
        'CreditNoteApplyToInvoices_service_1.CreditNoteApplyToInvoices, CreditUnapply])')
    marker = 'exports.CreditNotesApplyInvoiceController = CreditNotesApplyInvoiceController = __decorate(['
    return replace_once(source, marker, '''__decorate([
    (0, common_1.Delete)('applied-invoices/:creditNoteAppliedInvoiceId'),
    __param(0, (0, common_1.Param)('creditNoteAppliedInvoiceId')),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Number]),
    __metadata("design:returntype", Promise)
], CreditNotesApplyInvoiceController.prototype, "deleteAppliedInvoiceToCreditNote", null);
''' + marker)


def patch_credit_unapply_transaction(source):
    """Serialize repeated deletion of one allocation before decrementing balances."""
    marker = '// MAC-Bench: lock allocation before reversing its balances.'
    if marker in source:
        return source
    start = '        this.deleteApplyCreditNoteToInvoices = async (applyCreditToInvoicesId) => {\n'
    transaction = '            return this.uow.withTransaction(async (trx) => {\n'
    source = replace_once(source, transaction, '')
    source = replace_once(source, start, start + '            ' + marker + '\n' + transaction)
    source = replace_once(source, '.query()\n                .findById(applyCreditToInvoicesId);',
        '.query(trx)\n                .findById(applyCreditToInvoicesId)\n                .forUpdate();')
    return replace_once(source, '.query()\n                .findById(creditNoteAppliedToInvoice.creditNoteId)',
        '.query(trx)\n                .findById(creditNoteAppliedToInvoice.creditNoteId)')


def patch_vendor_unapply_transaction(source):
    """Apply the same reversal isolation to the existing vendor endpoint."""
    marker = '// MAC-Bench: lock vendor allocation before reversing its balances.'
    if marker in source:
        return source
    start = '    async deleteApplyVendorCreditToBills(appliedCreditToBillId) {\n'
    transaction = '        return this.uow.withTransaction(async (trx) => {\n'
    source = replace_once(source, transaction, '')
    source = replace_once(source, start, start + '        ' + marker + '\n' + transaction)
    source = replace_once(source, '.query()\n            .findById(appliedCreditToBillId);',
        '.query(trx)\n            .findById(appliedCreditToBillId)\n            .forUpdate();')
    return replace_once(source, '.query()\n            .findById(oldCreditAppliedToBill.vendorCreditId)',
        '.query(trx)\n            .findById(oldCreditAppliedToBill.vendorCreditId)')


def patch_credit_number(source):
    return replace_once(source,
        'const autoNextNumber = this.creditNoteAutoIncrement.getNextCreditNumber();',
        'const autoNextNumber = await this.creditNoteAutoIncrement.getNextCreditNumber();')


def patch_vendor_refund_create(source):
    marker = '// MAC-Bench: refund and credit balance share one transaction.'
    if marker in source:
        return source
    start = '        this.createRefund = async (vendorCreditId, refundVendorCreditDTO) => {\n'
    transaction = '            return this.uow.withTransaction(async (trx) => {\n'
    source = replace_once(source, transaction, '')
    source = replace_once(source, start, start + '            ' + marker + '\n' + transaction)
    source = replace_once(source,
        '.query()\n                .findById(vendorCreditId)',
        '.query(trx)\n                .findById(vendorCreditId)\n                .forUpdate()')
    source = replace_once(source, '.query()\n                    .insertAndFetch(refundCreditObj)',
                          '.query(trx)\n                    .insertAndFetch(refundCreditObj)')
    return replace_once(source,
        '                await this.eventPublisher.emitAsync(events_1.events.vendorCredit.onRefundCreated, {',
        '''                await this.vendorCreditModel().query(trx).findById(vendorCreditId)
                    .increment('refundedAmount', refundVendorCredit.amount);
                await this.eventPublisher.emitAsync(events_1.events.vendorCredit.onRefundCreated, {''')


def patch_vendor_refund_delete(source):
    marker = '// MAC-Bench: reverse the original refund and its balance together.'
    if marker in source:
        return source
    start = '    async deleteRefundVendorCreditRefund(refundCreditId) {\n'
    transaction = '        return this.uow.withTransaction(async (trx) => {\n'
    source = replace_once(source, transaction, '')
    source = replace_once(source, start, start + '        ' + marker + '\n' + transaction)
    source = replace_once(source, '.query()\n            .findById(refundCreditId)',
                          '.query(trx)\n            .findById(refundCreditId)\n            .forUpdate()')
    return replace_once(source,
        '            await this.refundVendorCreditModel()\n                .query(trx)',
        '''            await oldRefundCredit.$relatedQuery('vendorCredit', trx).forUpdate().first();
            await oldRefundCredit.$relatedQuery('vendorCredit', trx)
                .decrement('refundedAmount', oldRefundCredit.amount);
            await this.refundVendorCreditModel()
                .query(trx)''')


def patch_vendor_edit(source):
    marker = '// MAC-Bench: preserve settled vendor-credit amounts on edit.'
    if marker in source:
        return source
    start = '        this.editVendorCredit = async (vendorCreditId, vendorCreditDTO, trx) => {\n'
    transaction = '            return this.uow.withTransaction(async (trx) => {\n'
    source = replace_once(source, transaction, '''            const settled = Number(oldVendorCredit.refundedAmount || 0) + Number(oldVendorCredit.invoicedAmount || 0);
            if (Number(vendorCreditModel.amount) < settled) {
                throw new common_1.BadRequestException({ errors: [{ type: 'VENDOR_CREDIT_AMOUNT_SMALLER_THAN_USED', message: 'Amount cannot be less than credits already applied or refunded' }] });
            }
            if (settled > 0 && Number(vendorCreditDTO.vendorId) !== Number(oldVendorCredit.vendorId)) {
                throw new common_1.BadRequestException({ errors: [{ type: 'VENDOR_CREDIT_VENDOR_HAS_SETTLEMENTS', message: 'A settled vendor credit cannot change vendor' }] });
            }
''')
    source = replace_once(source, start, start + '            ' + marker + '\n' + transaction)
    return replace_once(source, '.query()\n                .findById(vendorCreditId)',
                        '.query(trx)\n                .findById(vendorCreditId)\n                .forUpdate()')


def patch_invoice_edit(source):
    marker = '// MAC-Bench: invoice edits retain paid, credited and written-off amounts.'
    if marker in source:
        return source
    start = '    async editSaleInvoice(saleInvoiceId, saleInvoiceDTO) {\n'
    transaction = '        return this.uow.withTransaction(async (trx) => {\n'
    source = replace_once(source, transaction, '')
    source = replace_once(source, start, start + '        ' + marker + '\n' + transaction)
    source = replace_once(source, '.query()\n            .findById(saleInvoiceId)',
                          '.query(trx)\n            .findById(saleInvoiceId)\n            .forUpdate()')
    source = replace_once(source, '.query()\n                .upsertGraphAndFetch(', '.query(trx)\n                .upsertGraphAndFetch(')
    return replace_once(source,
        '        this.validators.validateInvoiceAmountBiggerPaymentAmount(saleInvoiceObj.balance, oldSaleInvoice.paymentAmount);',
        '''        const settled = Number(oldSaleInvoice.paymentAmount || 0) + Number(oldSaleInvoice.creditedAmount || 0) + Number(oldSaleInvoice.writtenoffAmount || 0);
        if (Number(saleInvoiceObj.balance) < settled) {
            throw new common_1.BadRequestException({ errors: [{ type: 'INVOICE_AMOUNT_SMALLER_THAN_SETTLED', message: 'Invoice amount cannot be less than payments, applied credits and write-offs' }] });
        }
        if (settled > 0 && Number(saleInvoiceDTO.customerId) !== Number(oldSaleInvoice.customerId)) {
            throw new common_1.BadRequestException({ errors: [{ type: 'INVOICE_CUSTOMER_HAS_SETTLEMENTS', message: 'A settled invoice cannot change customer' }] });
        }''')


def patch_bill_edit(source):
    marker = '// MAC-Bench: bill edits retain paid and credited amounts.'
    if marker in source:
        return source
    start = '    async editBill(billId, billDTO) {\n'
    transaction = '        return this.uow.withTransaction(async (trx) => {\n'
    source = replace_once(source, transaction, '')
    source = replace_once(source, start, start + '        ' + marker + '\n' + transaction)
    source = replace_once(source, '.query()\n            .findById(billId)',
                          '.query(trx)\n            .findById(billId)\n            .forUpdate()')
    return replace_once(source,
        '        this.validators.validateBillAmountBiggerPaidAmount(billObj.amount, oldBill.paymentAmount);',
        '''        const settled = Number(oldBill.paymentAmount || 0) + Number(oldBill.creditedAmount || 0);
        if (Number(billObj.amount) < settled) {
            throw new common_1.BadRequestException({ errors: [{ type: 'BILL_AMOUNT_SMALLER_THAN_SETTLED', message: 'Bill amount cannot be less than payments and applied credits' }] });
        }
        if (settled > 0 && Number(billDTO.vendorId) !== Number(oldBill.vendorId)) {
            throw new common_1.BadRequestException({ errors: [{ type: 'BILL_VENDOR_HAS_SETTLEMENTS', message: 'A settled bill cannot change vendor' }] });
        }''')


def patch_vendor_allocation(source):
    start = '        this.applyVendorCreditToBills = async (vendorCreditId, applyCreditToBillsDTO) => {\n'
    source = replace_once(source, start, start + '''            // MAC-Bench: validate the native Object body before any mutation.
            const entries = applyCreditToBillsDTO?.entries;
            if (!Array.isArray(entries) || !entries.length ||
                entries.some(e => !e || !Number.isInteger(e.billId) || e.billId <= 0 ||
                    typeof e.amount !== 'number' || !Number.isFinite(e.amount) || e.amount <= 0) ||
                new Set(entries.map(e => e.billId)).size !== entries.length) {
                throw new common_1.BadRequestException({ errors: [{ type: 'INVALID_VENDOR_CREDIT_ALLOCATION', message: 'entries must contain unique positive billId and amount values' }] });
            }
''')
    source = replace_once(source,
        'this.validateBillsRemainingAmount(appliedBills, vendorCreditAppliedModel.amount);',
        'this.validateBillsRemainingAmount(appliedBills, vendorCreditAppliedModel.entries);')
    return replace_once(source,
        '''this.validateBillsRemainingAmount = (bills, amount) => {
            const invalidBills = bills.filter((bill) => bill.dueAmount < amount);''',
        '''this.validateBillsRemainingAmount = (bills, entries) => {
            const invalidBills = bills.filter((bill) => bill.dueAmount <
                entries.filter(entry => entry.billId === bill.id).reduce((total, entry) => total + entry.amount, 0));''')


def patch_transform(source: str) -> str:
    old = '                refundedAmount: 0,\n                invoicesAmount: 0,'
    new = '''                // MAC-Bench: initialize balances only on creation.
                ...(!oldCreditNote && { refundedAmount: 0, invoicesAmount: 0 }),'''
    if new in source:
        return source
    if source.count(old) != 1:
        raise ValueError('Credit note DTO balance initialization changed')
    return source.replace(old, new)


def patch_edit(source: str) -> str:
    marker = '// MAC-Bench: validate edits against the locked credit balance.'
    if marker in source:
        return source
    start = '    async editCreditNote(creditNoteId, creditNoteEditDTO) {\n'
    transaction = '        return this.uow.withTransaction(async (trx) => {\n'
    query = '.query()\n            .findById(creditNoteId)'
    if source.count(start) != 1 or source.count(transaction) != 1 or source.count(query) != 1:
        raise ValueError('Credit note edit transaction changed')
    source = source.replace(transaction, '''        if (Number(creditNoteModel.amount) < Number(oldCreditNote.creditsUsed)) {
            throw new common_1.BadRequestException({ errors: [{ type: 'CREDIT_NOTE_AMOUNT_SMALLER_THAN_USED', message: 'Credit note amount cannot be less than credits already applied or refunded' }] });
        }
        if (Number(oldCreditNote.creditsUsed) > 0 && Number(creditNoteEditDTO.customerId) !== Number(oldCreditNote.customerId)) {
            throw new common_1.BadRequestException({ errors: [{ type: 'CREDIT_NOTE_CUSTOMER_HAS_SETTLEMENTS', message: 'A credit note with applied or refunded credits cannot change customer' }] });
        }
''')
    source = source.replace(start, start + '        ' + marker + '\n' + transaction)
    return source.replace(query, '.query(trx)\n            .findById(creditNoteId)\n            .forUpdate()')


def patch_application(source: str) -> str:
    old = '''    editCreditNote(creditNoteId, creditNoteDTO) {
        return this.editCreditNoteService.editCreditNote(creditNoteId, creditNoteDTO);
    }'''
    new = '''    async editCreditNote(creditNoteId, creditNoteDTO) {
        // MAC-Bench: return the complete committed native document.
        await this.editCreditNoteService.editCreditNote(creditNoteId, creditNoteDTO);
        return this.getCreditNoteService.getCreditNote(creditNoteId);
    }'''
    if new in source:
        return source
    if source.count(old) != 1:
        raise ValueError('Credit note application edit endpoint changed')
    return source.replace(old, new)



def patch_expense_landed_cost_guard(source):
    """Prevent deletion of an expense referenced by an allocated landed cost."""
    source = replace_once(source, '''    async validateNoAssociatedLandedCost(expenseId) {
    }''', '''    async validateNoAssociatedLandedCost(expenseId, trx) {
        const associated = await this.billLandedCostModel().query(trx).findOne({
            fromTransactionType: 'Expense', fromTransactionId: expenseId,
        });
        if (associated) {
            throw new common_1.BadRequestException({
                errors: [{
                    type: constants_1.ERRORS.EXPENSE_HAS_ASSOCIATED_LANDED_COST,
                    message: 'This expense funds an allocated landed cost. Remove that allocation before deleting the expense.',
                }],
            });
        }
    }''')
    marker = 'exports.CommandExpenseValidator = CommandExpenseValidator = __decorate(['
    return replace_once(source, marker, '''__decorate([
    (0, common_1.Inject)('BillLandedCost')
], CommandExpenseValidator.prototype, "billLandedCostModel", void 0);
''' + marker)


def patch_expense_delete(source):
    """Check references and delete inside the same tenant transaction."""
    marker = '// MAC-Bench: expense reference check and deletion share a transaction.'
    if marker in source:
        return source
    source = replace_once(source, '        return this.uow.withTransaction(async (trx) => {\n', '')
    start = '    async deleteExpense(expenseId, trx) {\n'
    source = replace_once(source, start, start + '        ' + marker + '\n        return this.uow.withTransaction(async (trx) => {\n')
    source = replace_once(source, '.query()\n            .findById(expenseId)',
        '.query(trx)\n            .findById(expenseId)\n            .forUpdate()')
    return replace_once(source, 'await this.validator.validateNoAssociatedLandedCost(expenseId);',
        'await this.validator.validateNoAssociatedLandedCost(expenseId, trx);')


_INSTALL = 'import hashlib,json,pathlib,subprocess\n' + '\n'.join(
    inspect.getsource(fn) for fn in (patch_transform, patch_edit, patch_application,
        replace_once, patch_credit_number, patch_vendor_refund_create,
        patch_vendor_refund_delete, patch_vendor_edit, patch_invoice_edit,
        patch_bill_edit, patch_vendor_allocation, patch_credit_unapply_controller,
        patch_credit_unapply_transaction, patch_vendor_unapply_transaction,
        patch_expense_landed_cost_guard, patch_expense_delete)
) + r'''
root=pathlib.Path('/work/accounting-runtime');root.mkdir(exist_ok=True)
base='/app/server/packages/server/dist/modules/'
receipts=[]
for relative,patch in [
    ('Expenses/commands/CommandExpenseValidator.service.js',patch_expense_landed_cost_guard),
    ('Expenses/commands/DeleteExpense.service.js',patch_expense_delete),
    ('CreditNotes/commands/CommandCreditNoteDTOTransform.service.js',lambda s: patch_credit_number(patch_transform(s))),
    ('CreditNotes/commands/EditCreditNote.service.js',patch_edit),
    ('CreditNotes/CreditNoteApplication.service.js',patch_application),
    ('VendorCreditsRefund/commands/CreateRefundVendorCredit.service.js',patch_vendor_refund_create),
    ('VendorCreditsRefund/commands/DeleteRefundVendorCredit.service.js',patch_vendor_refund_delete),
    ('VendorCredit/commands/EditVendorCredit.service.js',patch_vendor_edit),
    ('SaleInvoices/commands/EditSaleInvoice.service.js',patch_invoice_edit),
    ('Bills/commands/EditBill.service.js',patch_bill_edit),
    ('VendorCreditsApplyBills/command/ApplyVendorCreditToBills.service.js',patch_vendor_allocation),
    ('CreditNotesApplyInvoice/CreditNotesApplyInvoice.controller.js',patch_credit_unapply_controller),
    ('CreditNotesApplyInvoice/commands/DeleteCreditNoteApplyToInvoices.service.js',patch_credit_unapply_transaction),
    ('VendorCreditsApplyBills/command/DeleteApplyVendorCreditToBill.service.js',patch_vendor_unapply_transaction),
]:
    local=root/pathlib.Path(relative).name
    remote='runtime-bigcapital:'+base+relative
    subprocess.run(['docker','cp',remote,str(local)],check=True,capture_output=True)
    before=local.read_bytes();after=patch(before.decode()).encode()
    local.write_bytes(after)
    subprocess.run(['docker','cp',str(local),remote],check=True,capture_output=True)
    receipts.append({'path':relative,'before_sha256':hashlib.sha256(before).hexdigest(),
                     'after_sha256':hashlib.sha256(after).hexdigest()})
print(json.dumps({'policy':'native_accounting_settlement_integrity','files':receipts}))
'''
