"""Public forms for the Arts Centre's existing administrative records.

Only field meanings and storage conventions are supplied; no solution choices,
target prices, task graph, verifier, private identities or initial DB are read.
"""
from copy import deepcopy


def text(description, **constraints):
    return {'type': 'string', 'minLength': 1, 'maxLength': 500,
            'description': description, **constraints}


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


def array(items, **constraints):
    return {'type': 'array', 'items': items, 'minItems': 1, 'maxItems': 100, **constraints}


def reference(path, entity, *, business=False, live=True):
    return {'path': path, 'entity': entity, 'identity_field': 'name' if entity.startswith('hrms.') else
            'slug' if entity == 'pretix.event' else 'id', 'business_id': business, 'live': live}


def specification():
    title = text('A readable title for this administrative record.', maxLength=250)
    amount = {'type': 'number', 'minimum': 0, 'description': 'The amount you determined from the business evidence; not calculated by this tool.'}
    currency = text('Currency code used by the obligation.', pattern='^[A-Z]{3}$', maxLength=3)
    date = text('Business date in YYYY-MM-DD format.', format='date', maxLength=10)
    native_id = text('Native Twenty record UUID, or an exact business reference from public/records.json.', maxLength=200)
    integer_id = {'description': 'Native integer ID, or its canonical decimal string.',
                  'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'string', 'pattern': '^(0|[1-9][0-9]*)$', 'maxLength': 30}]}
    room = {
        'id': 'room_booking', 'label': 'a room-booking record', 'entity': 'twenty.note',
        'schema': obj({'title': title,
            'course_id': text('Exact course business ID from public/records.json; stored as a business ID.'),
            'supplier_id': text('Exact venue-company business ID from public/records.json. The tool also links the note to this company.'),
            'amount': amount, 'currency': currency, 'due_date': date,
            'source_note_id': native_id,
            'calendar_event_ids': array(native_id, minItems=2, maxItems=2, uniqueItems=True)}),
        'constants': {'kind': 'cac_room_booking', 'status': 'confirmed'},
        'native_fields': {'title': 'title'},
        'body_fields': ['course_id', 'supplier_id', 'amount', 'currency', 'due_date',
                        'source_note_id', 'calendar_event_ids'],
        'company_link': 'supplier_id',
        'references': [reference('/course_id', 'pretix.event', business=True, live=False),
            reference('/supplier_id', 'twenty.company', business=True),
            reference('/source_note_id', 'twenty.note'),
            reference('/calendar_event_ids/*', 'twenty.calendar_event')]}
    follow = {
        'id': 'substitute_followup', 'label': 'a substitute-pay follow-up task', 'entity': 'twenty.task',
        'schema': obj({'title': title,
            'status': {'enum': ['TODO', 'IN_PROGRESS'], 'description': 'Leave this follow-up unfinished; payroll is not executed.'},
            'due_at': text('Follow-up deadline as an ISO 8601 date-time, with timezone.', format='date-time'),
            'employee_id': text('HRMS Employee name, or an exact public employee business reference. Stored as the HRMS name.'),
            'duty_id': text('HRMS Shift Assignment name, or its exact public business reference. No duty is created or submitted here.'),
            'amount': amount, 'currency': currency, 'source_note_id': native_id}),
        'constants': {'kind': 'cac_substitute_followup'},
        'native_fields': {'title': 'title', 'status': 'status', 'dueAt': 'due_at'},
        'body_fields': ['employee_id', 'duty_id', 'amount', 'currency', 'source_note_id'],
        'references': [reference('/employee_id', 'hrms.employee'), reference('/duty_id', 'hrms.shift_assignment'),
                       reference('/source_note_id', 'twenty.note')]}
    handover = {
        'id': 'financial_handover', 'label': 'a financial-handover record', 'entity': 'twenty.note',
        'schema': obj({'title': title, 'as_of': date,
            'room_booking_ids': array(native_id, uniqueItems=True),
            'substitute_task_id': native_id,
            'transferred_order': text('Original Pretix order code carrying the transferred entitlement. No enrolment is changed here.'),
            'pending_refunds': array(obj({'event': text('Pretix event slug; refund IDs are scoped within their order.'),
                'order': text('Original order code.'), 'refund_id': integer_id})),
            'preserved_college': obj({'bill_id': integer_id, 'payment_id': integer_id, 'credit_id': integer_id})}),
        'constants': {'kind': 'cac_financial_handover'},
        'native_fields': {'title': 'title'},
        'body_fields': ['as_of', 'room_booking_ids', 'substitute_task_id', 'transferred_order',
                        'pending_refunds', 'preserved_college'],
        'references': [reference('/room_booking_ids/*', 'twenty.note'), reference('/substitute_task_id', 'twenty.task')]}
    sample_note = '00000000-0000-4000-8000-000000000001'
    booking_scope = (
        "Twenty is the centre's booking register for this task. Saving the booking and its venue-company link "
        'registers an internally confirmed, unpaid booking under a signed offer. '
        'You must match the offer and meet the budget and scheduling requirements. '
        'Update course sessions and teaching calendars separately; this tool does not make payments.')
    room['operation_descriptions'] = {
        'create': 'Register a room booking by creating its Twenty note and venue-company link. ' + booking_scope,
        'read': "Read an existing room booking from the centre's Twenty register, including its stored venue reference, offer reference and calendar IDs.",
        'update': 'Revise an existing room booking and reconcile its venue-company link. ' + booking_scope}
    follow['operation_descriptions'] = {
        'create': 'Create a pending substitute-pay follow-up task in Twenty with the supplied employee, duty, amount and deadline. '
                  'This records an unpaid obligation; it does not create or submit the teaching duty or execute payroll.',
        'read': 'Read the current substitute-pay follow-up task and its stored obligation details.',
        'update': 'Revise the pending substitute-pay follow-up task and deadline. '
                  'This does not change the teaching duty or execute payroll.'}
    handover['operation_descriptions'] = {
        'create': 'Save a dated financial handover in Twenty linking the supplied bookings, pay follow-up, tuition entitlement and pending refunds. '
                  'This records the handover; it does not transfer tuition, issue refunds or post accounting transactions.',
        'read': 'Read an existing financial handover and its stored business references.',
        'update': 'Revise the financial handover date and business references. '
                  'This does not change enrolments, refunds, payroll or accounting transactions.'}
    sample_calendar = ['00000000-0000-4000-8000-000000000002', '00000000-0000-4000-8000-000000000003']
    room['example'] = dict(title='Museum workshop booking', course_id='museum_workshop',
        supplier_id='museum_annex', amount=42, currency='USD', due_date='2030-06-15',
        source_note_id=sample_note, calendar_event_ids=sample_calendar)
    follow['example'] = dict(title='Museum substitute-pay follow-up', status='TODO', due_at='2030-06-30T17:00:00Z',
        employee_id='DOC-EMP-001', duty_id='DOC-DUTY-001', amount=25, currency='USD', source_note_id=sample_note)
    handover['example'] = dict(title='Museum financial handover', as_of='2030-06-10', room_booking_ids=[sample_note],
        substitute_task_id='00000000-0000-4000-8000-000000000004', transferred_order='DOCORDER',
        pending_refunds=[dict(event='museum-workshop', order='DOCORDER', refund_id=1)],
        preserved_college=dict(bill_id=1, payment_id=2, credit_id=3))
    return deepcopy({'format': 'mac_bench.record_forms.v1', 'forms': [room, follow, handover]})
