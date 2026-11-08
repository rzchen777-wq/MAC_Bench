"""Reviewed Pretix actions over the captured Order/Payment/Refund handlers.

The native endpoints remain the execution path. These definitions describe their
business effects and fields read outside DRF serializers. Source: the pinned
pretix library in pretix_queries.LIBRARY; evidence is retained in the catalog.
"""
from copy import deepcopy

from ..pretix_queries import LIBRARY

ORDER = 'pretix.order'
PAYMENT = 'pretix.order_payment'
REFUND = 'pretix.order_refund'
POSITION = 'pretix.order_position'

ACTIONS = {
    (REFUND, 'create'): (
        'Record a refund for an order in the supplied state, optionally linked to an original payment.',
        ('This records the refund; it does not call the payment provider to transfer money. '
         'payment is the original payment local_id within this order. '
         'To record completion of a created/transit refund, use its done action.',)),
    (REFUND, 'done'): (
        'Mark an existing created or in-transit refund as completed.',
        ('Requires state=created or transit; returns the refund in state=done. '
         'This records completion rather than initiating a provider refund.',)),
    (REFUND, 'process'): (
        'Acknowledge an external refund and update the parent order status.',
        ('Requires state=external; marks the refund done. mark_refunded=true cancels the parent order; '
         'otherwise it becomes pending unless it is already paid with no amount due. Read the order afterwards.',)),
    (REFUND, 'cancel'): (
        'Cancel a refund that is created, in transit or awaiting external processing.',
        ('Requires state=created, transit or external; returns state=canceled. '
         'This does not reverse a completed refund or cancel the parent order.',)),
    (PAYMENT, 'create'): (
        'Add a payment record to an order, optionally requesting confirmation.',
        ('When state=confirmed is requested, native confirmation is attempted. '
         'A quota conflict can leave the new payment pending even though creation succeeds; inspect the returned state.',)),
    (PAYMENT, 'confirm'): (
        'Confirm an existing created or pending payment and update its order.',
        ('Requires state=created or pending. Confirmation checks native availability and payment rules. '
         'Read the returned payment state and the parent order for the resulting balance and status.',)),
    (PAYMENT, 'cancel'): (
        'Cancel a created or pending payment attempt through its payment provider.',
        ('Requires state=created or pending. This cancels an unconfirmed attempt; use a refund for an already confirmed payment.',)),
    (PAYMENT, 'refund'): (
        'Create and execute a refund against a confirmed payment through its provider.',
        ('Requires a confirmed payment, a positive amount within its remaining refundable amount, '
         'and provider support for the requested full or partial refund. Returns a refund record, not a payment.',
         'If provider execution fails after creation, a failed refund record may remain. Read the order refunds before continuing.',
         'If the refund leaves the order owing money, mark_refunded=true cancels the order; otherwise it becomes pending.')),
    (ORDER, 'mark_paid'): (
        'Confirm a manual payment for the outstanding amount of a pending or expired order.',
        ('Reuses a matching unconfirmed manual payment, or cancels other unconfirmed attempts and creates one. '
         'Confirmation can fail on availability; read payments before repeating this operation.',)),
    (ORDER, 'mark_pending'): (
        'Change a paid order back to pending payment.',
        ('Requires order status=p; sets status=n. Existing payment records are preserved; this is not a refund.',)),
    (ORDER, 'mark_expired'): (
        'Expire a pending order through its native lifecycle.',
        ('Requires order status=n; sets status=e and applies native expiration effects.',)),
    (ORDER, 'mark_canceled'): (
        'Cancel an eligible order, with an optional cancellation fee and customer notification.',
        ('The native cancellation rules determine eligibility. Read the order for its final status and total; '
         'refunds of original payments are separate operations.',)),
    (ORDER, 'mark_refunded'): (
        'Cancel a paid order using the native refunded-order transition.',
        ('Requires order status=p. Despite the action name, this changes the order lifecycle; '
         'it does not create or execute a payment refund.',)),
    (ORDER, 'reactivate'): (
        'Reactivate a canceled order through native availability checks.',
        ('Requires order status=c. Read the returned order for the resulting status and expiry.',)),
    (ORDER, 'extend'): (
        'Set a new payment deadline for an order, rechecking availability where required.',
        ('expires is a date, interpreted as 23:59:59 in the event timezone. '
         'This changes the payment deadline, not the event or training date.',)),
    (ORDER, 'approve'): (
        'Approve an order awaiting organizer approval.',
        ('Applies native approval and availability rules; payment may still be required. Read status and require_approval in the returned order.',)),
    (ORDER, 'deny'): (
        'Reject an order awaiting organizer approval.',
        ('Applies the native denial lifecycle and returns the order. send_email controls the customer notification.',)),
    (ORDER, 'change'): (
        'Apply a set of position and fee changes to one order.',
        ('Supports creating, changing, canceling and splitting positions, changing fees and recalculating taxes. '
         'Changes to a position or fee that is canceled in the same request are skipped. '
         'Returns the updated order after native validation and commit.',)),
    (ORDER, 'regenerate_secrets'): (
        'Replace the order access secret and ticket secrets for its positions.',
        ('Invalidates existing ticket secrets and cached tickets; returns the updated order.',)),
    (ORDER, 'resend_link'): (
        'Send the order access link to the email address on the order.',
        ('Requires an order email address. Success has no record body; this does not change the booking or confirm email delivery.',)),
    (POSITION, 'add_block'): (
        'Add a named block to a ticket position.',
        ('Uses the native order-change manager and returns the updated position. '
         'The block name is supplied in the body; this does not cancel the order.',)),
    (POSITION, 'remove_block'): (
        'Remove a named block from a ticket position.',
        ('Uses the native order-change manager and returns the updated position.',)),
    (POSITION, 'price_calc'): (
        'Calculate the ticket price for a proposed product, occurrence or tax-rule change.',
        ('Returns gross, net and tax amounts without applying the proposed booking change. '
         'Use a position update or order change to modify the booking.',)),
    (POSITION, 'printlog'): (
        'Record a ticket-printing event for a position.',
        ('Returns the created print-log entry. This records a printing event; it does not generate or print a ticket.',)),
    (POSITION, 'regenerate_secrets'): (
        'Replace the ticket secret for one position through the native order-change manager.',
        ('Returns the updated position. Use its current secret for subsequent ticket operations.',)),
    (POSITION, 'redeem'): (
        'Record ticket entry or exit against a check-in list.',
        ('pk is the ticket barcode or secret, not the numeric position id. list identifies the check-in list. '
         'type defaults to entry; force and ignore_unpaid affect native admission checks. '
         'Inspect the native check-in result to determine whether admission was accepted.',)),
    (POSITION, 'get'): (
        'Search ticket positions through the organizer check-in search endpoint.',
        ('Returns the native search results within the organizer scope; this is not a single-position lookup.',)),
    ('pretix.quota', 'availability'): (
        'Read current capacity and availability counts for one quota.',
        ('Returns available, available_number, total_size and native occupancy counts. '
         'This checks capacity without reserving places. In this application version, waiting_list repeats pending_orders.',)),
    ('pretix.customer', 'anonymize'): (
        'Anonymize the customer record through the native customer operation.',
        ('Returns the customer after anonymization. This modifies personal data; it is not a read or a booking cancellation.',)),
}

SEND_EMAIL = {'type': 'boolean', 'default': True,
              'description': 'Send the native customer notification for this action. Defaults to true.'}
FORCE = {'type': 'boolean', 'default': False,
         'description': 'Ask the native operation to override availability checks. Task capacity requirements still apply.'}
MARK_REFUNDED = {'type': 'boolean', 'default': False,
                 'description': 'Cancel the parent order through the refunded-order transition. This flag does not itself transfer money.'}
MARK_PENDING = {'type': 'boolean', 'default': False,
                'description': 'If the order is paid but now owes money, change it to pending. Ignored when mark_refunded is true.'}
MONEY = {'type': ['string', 'number'],
         'description': 'Amount in the event currency, with at most two decimal places.'}
COMMENT = {'type': 'string', 'description': 'Comment recorded with this action.'}
FIELDS = {
    (PAYMENT, 'create'): {'send_email': SEND_EMAIL, 'force': FORCE},
    (PAYMENT, 'confirm'): {'send_email': SEND_EMAIL, 'force': FORCE},
    (PAYMENT, 'refund'): {
        'amount': {**MONEY, 'description': 'Amount to refund in this call, in the event currency; positive and within the refundable balance. '
                   'If omitted, defaults to the original payment amount, not the remaining amount after prior refunds.'},
        'comment': COMMENT, 'mark_refunded': MARK_REFUNDED},
    (REFUND, 'create'): {'mark_refunded': MARK_REFUNDED, 'mark_pending': MARK_PENDING},
    (REFUND, 'process'): {'mark_refunded': MARK_REFUNDED},
    (ORDER, 'mark_paid'): {'send_email': SEND_EMAIL},
    (ORDER, 'mark_canceled'): {'send_email': SEND_EMAIL, 'comment': {**COMMENT, 'type': ['string', 'null']},
                              'cancellation_fee': {**MONEY, 'type': ['string', 'number', 'null']}},
    (ORDER, 'extend'): {'force': FORCE, 'expires': {'type': 'string', 'format': 'date', 'pattern': r'^\d{4}-\d{2}-\d{2}$',
        'description': 'New payment-deadline date (YYYY-MM-DD), through the end of that day in the event timezone.'}},
    (ORDER, 'approve'): {'send_email': SEND_EMAIL},
    (ORDER, 'deny'): {'send_email': SEND_EMAIL, 'comment': COMMENT},
}

# Exact messages from guards before any mutation in the captured handlers.
# Later provider errors, mixed payloads and unknown failures are not classified.
STATE_GUARDS = {
    (REFUND, 'done'): 'Invalid state of refund',
    (REFUND, 'process'): 'Invalid state of refund',
    (REFUND, 'cancel'): 'Invalid state of refund',
    (PAYMENT, 'confirm'): 'Invalid state of payment',
    (PAYMENT, 'cancel'): 'Invalid state of payment',
    (PAYMENT, 'refund'): 'Invalid state of payment.',
    (ORDER, 'mark_pending'): 'The order is not paid.',
    (ORDER, 'mark_expired'): 'The order is not pending.',
    (ORDER, 'mark_paid'): 'The order is not pending or expired.',
    (ORDER, 'mark_refunded'): 'The order is not paid.',
    (ORDER, 'reactivate'): 'The order is not allowed to be reactivated.',
    (ORDER, 'mark_canceled'): 'The order is not allowed to be canceled.',
}


def refine_contract(binding, operation):
    key = binding['entity'], operation.get('action')
    if operation['input']['library'] != LIBRARY or key not in ACTIONS:
        return operation
    result = deepcopy(operation)
    result['usage_description'], result['usage_notes'] = ACTIONS[key]
    fields = result['input'].setdefault('inline_fields', {})
    # Only the canonical native flag is exposed. No adapter-side alias/translation.
    if key in {(PAYMENT, 'refund'), (REFUND, 'create'), (REFUND, 'process')}:
        fields.pop('mark_canceled', None)
    for name, schema in FIELDS.get(key, {}).items():
        fields[name] = {**fields.get(name, {}), 'location': 'body',
                        'access': 'required' if key == (ORDER, 'extend') and name == 'expires' else 'optional',
                        'public_schema': deepcopy(schema)}
    return result


def no_write_rejection(binding, operation, exc):
    if operation['input']['library'] != LIBRARY:
        return False
    message = STATE_GUARDS.get((binding['entity'], operation.get('action')))
    return bool(message and exc.status == 400 and exc.native_payload == {'detail': message})


def state_rejection(tool, payload, status):
    operation = tool.native_contract.get('operation_contract', {})
    if operation.get('input', {}).get('library') != LIBRARY:
        return None
    key = tool.entity, operation.get('action')
    message = STATE_GUARDS.get(key)
    if not message or status != 400 or payload != {'detail': message}:
        return None
    return {'code': 'native_state_rejected', 'http_status': status,
            'field_errors': [{'field': 'status' if tool.entity == ORDER else 'state', 'rule': 'invalid_state'}],
            'message': ACTIONS[key][1][0] + ' This request was rejected before changing data.',
            'retry_guidance': 'Read the current object and select an action allowed in that state. '
                              'A new request ID does not change the lifecycle requirement.'}
