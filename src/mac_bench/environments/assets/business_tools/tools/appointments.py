"""One appointment and its explicitly supplied native calendar relationships."""
from copy import deepcopy
from datetime import datetime

from jsonschema import Draft202012Validator, FormatChecker

from .compound import CompoundOperation, Steps, StepStopped, native_operations
from .contracts import Tool, ToolError, object_schema
from .registry import ToolRegistry

EVENT = 'twenty.calendar_event'
CHANNEL = 'twenty.calendar_channel'
LINK = 'twenty.calendar_channel_event_association'
PARTICIPANT = 'twenty.calendar_event_participant'
PERSON = 'twenty.person'
EVENT_FIELDS = ['id', 'title', 'startsAt', 'endsAt', 'location', 'description', 'isCanceled', 'isFullDay']
PART_FIELDS = ['id', 'personId', 'handle', 'displayName', 'isOrganizer', 'responseStatus']


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class Appointment(CompoundOperation):
    def __init__(self, action, operations, schema):
        self.action, self.operations, self.schema = action, operations, schema

    def execute_agent(self, agent, arguments, call_id):
        steps = Steps(agent, call_id, self.operations)
        identity = arguments.get('event_id')
        if identity:
            steps.ids['event_id'] = identity
        try:
            errors = list(Draft202012Validator(self.schema, format_checker=FormatChecker()).iter_errors(arguments))
            if errors:
                raise ToolError('invalid_arguments', 'Invalid appointment format at ' + '.'.join(map(str, errors[0].path)))
            before = steps.one(EVENT, identity, EVENT_FIELDS) if identity else {}
            if self.action == 'read':
                return self.readback(steps, identity)
            data = {k: deepcopy(arguments[k]) for k in EVENT_FIELDS if k != 'id' and k in arguments}
            current = {**before, **data}
            if not current.get('startsAt') or not current.get('endsAt') or stamp(current['endsAt']) <= stamp(current['startsAt']):
                raise ToolError('invalid_appointment_interval', 'Supply timezone-aware startsAt and endsAt, with endsAt later than startsAt.')
            channel_id = arguments.get('calendar_channel_id')
            if channel_id:
                channel = steps.one(CHANNEL, channel_id, ['id', 'visibility'])
                if channel.get('visibility') != 'SHARE_EVERYTHING':
                    raise ToolError('calendar_not_workspace_visible', 'This helper needs an explicitly selected SHARE_EVERYTHING calendar. Native tools remain available for other visibility modes.')
            people = arguments.get('participants', [])
            if len({p['personId'] for p in people}) != len(people):
                raise ToolError('duplicate_participant', 'Supply each personId at most once.')
            for person in people:
                steps.one(PERSON, person['personId'], ['id'])
            # Read relationships before the first write so repeat update is
            # idempotent in native objects, not merely in the receipt journal.
            links = self.links(steps, identity) if identity and channel_id else []
            participants = self.participants(steps, identity) if identity and people else []
            for person in people:
                if sum(p.get('personId') == person['personId'] for p in participants) > 1:
                    raise ToolError('ambiguous_participant', 'Duplicate native participants exist. Inspect and repair with native tools first.')
            if self.action == 'create':
                data.setdefault('isCanceled', False)
                result = steps.invoke(EVENT, 'createOne', {'data': data, 'fields': EVENT_FIELDS})
                identity = steps.confirmed_id(result, 'event_id')
            elif data:
                steps.invoke(EVENT, 'updateOne', {'id': identity, 'data': data, 'fields': EVENT_FIELDS})
            if channel_id and not any(row.get('calendarChannelId') == channel_id for row in links):
                steps.invoke(LINK, 'createOne', {'data': {'calendarEventId': identity,
                    'calendarChannelId': channel_id, 'eventExternalId': 'manual-' + identity,
                    'recurringEventExternalId': ''}, 'fields': ['id', 'calendarEventId', 'calendarChannelId']})
            for person in people:
                old = next((p for p in participants if p.get('personId') == person['personId']), None)
                if old is None:
                    steps.invoke(PARTICIPANT, 'createOne', {'data': {'calendarEventId': identity, **person}, 'fields': PART_FIELDS})
                elif any(old.get(k) != v for k, v in person.items()):
                    steps.invoke(PARTICIPANT, 'updateOne', {'id': old['id'], 'data': person, 'fields': PART_FIELDS})
            actual = self.readback(steps, identity)
            saved = actual['data']['event']
            for k, v in data.items():
                equal = stamp(saved[k]) == stamp(v) if k in ('startsAt', 'endsAt') and saved.get(k) else saved.get(k) == v
                if not equal:
                    raise ToolError('appointment_readback_mismatch', 'The saved appointment differs from a supplied field. Inspect live state.')
            if channel_id and not any(x.get('calendarChannelId') == channel_id for x in actual['data']['calendar_links']):
                raise ToolError('calendar_link_missing', 'The supplied calendar relationship was not confirmed.')
            for person in people:
                if not any(all(p.get(k) == v for k, v in person.items()) for p in actual['data']['participants']):
                    raise ToolError('participant_readback_mismatch', 'A supplied participant relationship was not confirmed.')
            return actual
        except (ToolError, StepStopped) as exc:
            return steps.failure(exc)

    def links(self, steps, identity):
        return steps.many(LINK, {'calendarEventId': {'eq': identity}},
            ['edges.node.id', 'edges.node.calendarChannelId'])

    def participants(self, steps, identity):
        return steps.many(PARTICIPANT, {'calendarEventId': {'eq': identity}},
            ['edges.node.' + k for k in PART_FIELDS])

    def readback(self, steps, identity):
        return steps.success(event=steps.one(EVENT, identity, EVENT_FIELDS),
            calendar_links=self.links(steps, identity), participants=self.participants(steps, identity))


def extend(registry):
    operations = native_operations(registry, {EVENT, CHANNEL, LINK, PARTICIPANT, PERSON})
    tools = list(registry.tools())
    common = {(EVENT, 'findOne'), (LINK, 'findMany'), (PARTICIPANT, 'findMany')}
    writes = {(CHANNEL, 'findOne'), (PERSON, 'findOne'), (LINK, 'createOne'),
              (PARTICIPANT, 'createOne'), (PARTICIPANT, 'updateOne')}
    for action in ('create', 'read', 'update'):
        required_ops = common | (set() if action == 'read' else writes | {(EVENT, action + 'One')})
        if not required_ops <= operations.keys():
            continue
        # All participants/links must belong to this same native workspace.
        from .contracts import encoded
        if len({encoded(operations[k].scope) for k in required_ops}) != 1:
            continue
        props, required = {}, []
        if action != 'create':
            props['event_id'] = {'type': 'string', 'minLength': 1}
            required.append('event_id')
        if action != 'read':
            props.update({k: {'type': 'string'} for k in ('title', 'location', 'description')})
            props.update({k: {'type': 'string', 'format': 'date-time',
                'description': 'Explicit ISO 8601 timestamp with timezone, from the task business schedule; no current-date default.'}
                          for k in ('startsAt', 'endsAt')})
            props.update({k: {'type': 'boolean'} for k in ('isCanceled', 'isFullDay')})
            props['calendar_channel_id'] = {'type': 'string', 'minLength': 1,
                'description': 'Explicit native SHARE_EVERYTHING calendar-channel UUID. Adds this association without removing other calendars.'}
            props['participants'] = {'type': 'array', 'maxItems': 1000, 'items': object_schema({
                'personId': {'type': 'string', 'minLength': 1},
                'handle': {'type': 'string', 'minLength': 1}, 'displayName': {'type': 'string'},
                'isOrganizer': {'type': 'boolean'},
                'responseStatus': {'enum': ['NEEDS_ACTION', 'DECLINED', 'TENTATIVE', 'ACCEPTED']}},
                ['personId', 'handle', 'isOrganizer']),
                'description': 'Add/update explicitly supplied people by personId. Omitted people are preserved. No identity, availability or acceptance is inferred.'}
            if action == 'create':
                required += ['title', 'startsAt', 'endsAt', 'calendar_channel_id']
        schema = object_schema(props, required)
        base = operations[EVENT, 'findOne' if action == 'read' else action + 'One']
        tools.append(Tool('calendar.appointment.' + action, 'twenty', 'twenty.appointment', action,
            base.binding, base.scope, action.capitalize() + ' a native calendar appointment with explicitly supplied calendar/participant links. '
            'This does not reserve a venue, submit staffing duties, change enrolment or infer permission.',
            schema, 'data contains event_id, event, calendar_links, participants and native step receipts.',
            action == 'read', Appointment(action, operations, schema),
            ('Create explicitly links the event to the selected visible calendar. Update preserves omitted fields and existing relationships.',
             'Supplied participants are upserted by personId; omitted participants/calendars are not deleted. Use native operations for removals.',
             'Operations are not atomic across native steps. partial/unknown return confirmed IDs; inspect live state before recovery.',
             'Read is a live observation. Success does not prove schedule feasibility, authorization or business completion.'),
            {'output': {'event_id': 'Native event UUID', 'steps': 'Attributed native receipts'}}))
    return ToolRegistry(tools, catalog_revision=registry.catalog_revision)
