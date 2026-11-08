"""Regression cases against the SQL actually shipped with current business tasks."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from mac_bench.environments.tools.contracts import ToolError, validate
from mac_bench.environments.tools.data_workspace import execute_sql
from mac_bench.environments.verification.sql import QueryEngine, json_keys_match

from mac_bench.runtime.paths import repository_root
import os

TASKS = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root() / 'data/tasks'))


def task(number):
    p = TASKS / f'op26_project_content_operations_{number:03}'
    return p, json.loads((p / 'private/verifier.json').read_text())


def note(current, title, body):
    current['twenty_note'].append({'id': 'regression-' + title, 'title': title,
        'deletedAt': None, 'bodyV2': {'markdown': body if isinstance(body, str) else json.dumps(body)}})


def check(number, current, ids):
    _, v = task(number)
    with QueryEngine(v['initial'], current, v['identities'], v['layout'],
                            facts=v['facts'], links=v['links'], max_steps=20_000_000) as db:
        results = {c['id']: db.check(c['sql'], c['binding']['collections'], assertion=c['assertion'])
                   for c in v['checks'] if c['id'] in ids}
    assert set(results) == set(ids)
    return all(r['passed'] for r in results.values())


@pytest.mark.parametrize('variant,valid', [('plain', True), ('escape', True), ('reordered', True),
    ('extra', False), ('extra_escape', False), ('duplicate', False), ('top_extra', False)])
def test_archive_export_keys_use_decoded_json(variant, valid):
    _, v = task(7)
    rows = []
    for r in v['facts']['records']:
        if r['id'] != r['canonical_id'] or not r['public_allowed'] or r['embargo'] > '2027-04-01':
            continue
        text = r['original_text']
        if r.get('correction_before'):
            text = text.replace(r['correction_before'], r['correction_after'])
        if r.get('redaction_span'):
            text = text.replace(r['redaction_span'], r['redaction_replacement'])
        rows.append(dict(id=r['id'], title=r['correct_title'], year=r['correct_year'],
                         kind=r['kind'], collection=r['collection'], attribution=r['attribution'], text=text))
    if variant.startswith('extra'):
        rows[0]['private_original'] = 'Not authorized for export'
    doc = {'entries': rows}
    if variant == 'top_extra':
        doc['private_original'] = 'Not authorized'
    raw = json.dumps(doc, sort_keys=variant == 'reordered', indent=2)
    if 'escape' in variant:
        raw = raw.replace(json.dumps(rows[0]['text']), json.dumps(rows[0]['text']).replace(':', r'\u003a', 1))
    if variant == 'duplicate':
        raw = raw.replace('"entries":', '"entries": [], "entries":', 1)
    current = deepcopy(v['initial']); note(current, 'ELHA RESULT public_export', raw)
    ids = [c['id'] for c in v['checks'] if c['id'].startswith('public_export:')]
    assert check(7, current, ids) is valid


@pytest.mark.parametrize('extra', [False, True])
def test_sponsor_extract_accepts_colons_but_no_extra_fields(extra):
    _, v = task(5)
    rows = [dict(block_id=b['id'], offset_seconds=0, seconds=20, wording='Acknowledgment: approved')
            for b in v['facts']['blocks']]
    placements = [dict(r, sponsor_id='SP001') for r in rows]
    if extra:
        rows[0]['private_script'] = 'Unapproved'
    current = deepcopy(v['initial'])
    note(current, 'NCR RESULT sponsors', {'placements': placements, 'placement_extract': rows})
    assert check(5, current, ['sponsors:limited_approval_extract']) is (not extra)


@pytest.mark.parametrize('variant,valid', [('plain', True), ('escape', True), ('extra', False), ('top_extra', False), ('duplicate', False)])
def test_custody_field_contract(variant, valid):
    _, v = task(8)
    row = dict(id='VIS001', route_id='ROUTE001', cooler_id='COOL001', labels=['b:1','b:2','b:3'],
               temperature_c=4, status='planned', collector_shift_id='shift:1', courier_shift_id='shift:2')
    if variant == 'extra':
        row['result'] = 0.4
    doc = {'samples': [row]}
    if variant == 'top_extra':
        doc['measured'] = True
    raw = json.dumps(doc)
    if variant == 'escape':raw = raw.replace('shift:', r'shift\u003a')
    if variant == 'duplicate':raw = raw.replace('"samples":', '"samples": [], "samples":', 1)
    current = deepcopy(v['initial']); note(current, 'CCSP RESULT custody', raw)
    assert check(8, current, ['custody:no_fabricated_sample_results', 'custody:complete_register']) is valid


def river_notice():
    _, v = task(6); current = deepcopy(v['initial'])
    sessions, work, notices = [], [], []
    for site in v['facts']['sites']:
        source = next(s for s in v['facts']['sessions'] if s['site_id'] == site['id'])
        session = dict(id=source['id'], site_id=site['id'], zone=site['zone'],
                       activity=site['activities'][0], starts_at=source['starts_at'], ends_at=source['ends_at'])
        sessions.append(session)
        for kind in ('leader', 'first_aid'):
            work.append(dict(id=kind+source['id'], kind=kind, session_id=source['id'],
                             native_shift_id=kind+source['id']))
        ref = lambda k: v['identities'][k]['identity']['id']
        notices.append(dict(id=site['id'], company_id=ref('site:'+site['id']),
            contact_id=ref('landowner_contact:'+site['contact_id']),
            notice_date='2027-04-01', meeting_point=site['meeting_point'], closed_zone=site['closed_zone'],
            sessions=[dict(session_id=session['id'], zone=session['zone'], activity=session['activity'],
                starts_at=session['starts_at'], ends_at=session['ends_at'],
                leader_shift_id='leader'+source['id'], first_aid_shift_id='first_aid'+source['id'])]))
    note(current,'RCC RESULT programme',{'sessions':sessions})
    note(current,'RCC RESULT resources',{'work':work})
    return current, notices


@pytest.mark.parametrize('mutation', ['valid','wrong_zone','wrong_time','missing_supervision','missing_sessions','wrong_closed_zone','empty_shell'])
def test_landowner_handover_checks_arrangements(mutation):
    current, notices = river_notice()
    item = notices[0]
    if mutation == 'wrong_zone':item['sessions'][0]['zone']='nesting_bank'
    if mutation == 'wrong_time':item['sessions'][0]['starts_at']='2027-05-01T00:00:00Z'
    if mutation == 'missing_supervision':item['sessions'][0]['first_aid_shift_id']='nonexistent'
    if mutation == 'missing_sessions':item.pop('sessions')
    if mutation == 'wrong_closed_zone':item['closed_zone']='upland'
    if mutation == 'empty_shell':item.pop('sessions');item['instructions']='x'
    note(current,'RCC RESULT instructions',{'groups':[], 'landowners':notices})
    assert check(6,current,['instructions:complete_landowner_handover']) is (mutation == 'valid')


@pytest.mark.parametrize('briefing', ['An introductory briefing on hand tools is required.', 'Short optional reminder.'])
def test_group_prose_is_separate_from_source_requirements(briefing):
    _, v = task(6); current = deepcopy(v['initial'])
    g, s, site = v['facts']['groups'][0], v['facts']['sessions'][0], v['facts']['sites'][0]
    note(current,'RCC RESULT programme', {'sessions':[dict(id=s['id'], site_id=site['id'], activity='planting', starts_at=s['starts_at'])]})
    note(current,'RCC RESULT groups', {'placements':[dict(id=g['id'], session_id=s['id'])]})
    row=dict(id=g['id'], session_id=s['id'], contact_name=g['current_contact_name'], meeting_point=site['meeting_point'],
        starts_at=s['starts_at'], activity='planting', source_briefing=g['briefing'], source_personal_kit=g['required_personal_kit'],
        source_transport_condition=g['transport_condition'], transport_claim_cap=g['transport_claim_cap'],
        accessible_route=site['accessible_route'], briefing=briefing)
    note(current,'RCC RESULT instructions',{'groups':[row], 'landowners':[]})
    assert check(6,current,['instructions:group_handover'])
    row.pop('source_briefing');current['twenty_note'].pop();note(current,'RCC RESULT instructions',{'groups':[row]})
    assert not check(6,current,['instructions:group_handover'])


def test_register_public_status_enum_is_enforced():
    p=TASKS/'op26_service_administration_007/public/registers.json'
    catalog=json.loads(p.read_text())
    spec=next(s for s in catalog['registers'] if s['id']=='bppa_result_eligibility')['schema']['properties']['children']['items']
    for key in ('status','reason'):
        with pytest.raises(ToolError,match='expected one of'):
            validate(spec['properties'][key],'banana')
        for choice in spec['properties'][key]['enum']:
            validate(spec['properties'][key],choice)


def test_compute_mode_error_is_actionable_and_no_alias():
    with pytest.raises(ToolError,match='2 rows and 2 columns') as exc:
        execute_sql({},'SELECT 1 AS a, 2 AS b UNION ALL SELECT 3,4',output='json_value')
    assert 'output="rows"' in str(exc.value) and 'read-only' in str(exc.value)
    with pytest.raises(ToolError,match='rows or json_value'):
        execute_sql({},'SELECT 1',output='json')


@pytest.mark.parametrize('value', ['{"a":1,"a":2}', '{"a":NaN}', '{"a":{"b":1,"b":2}}'])
def test_key_match_rejects_ambiguous_documents(value):
    assert json_keys_match(value,'["a"]') == 0


def test_native_invalid_choice_exposes_public_allowed_values():
    from types import SimpleNamespace
    from mac_bench.environments.errors import NativeHTTPFailure
    from mac_bench.environments.tools.error_feedback import feedback
    tool = SimpleNamespace(native_contract={'fields': {'status': {}}}, read_only=False,
        input_schema={'properties': {'body': {'properties': {'status': {'enum': ['draft', 'approved']}}}}})
    result = feedback(NativeHTTPFailure('POST', 400, {'status': ['not a valid choice SECRET']}), tool)
    assert result['field_errors'] == [{'field':'status', 'rule':'invalid_choice', 'allowed_values':['draft','approved']}]
    assert 'SECRET' not in json.dumps(result)


def test_long_record_keeps_identity_preview_and_full_text_path():
    from mac_bench.environments.tools.navigation import page
    document = {'rows': [{'id': 'A', 'text': '正文' * 10000}]}
    first = page(document, identity='test', path='/rows', compact=True)
    entry = first['items'][0]
    assert entry['preview'] == {'id':'A'} and not entry['complete']
    text = page(document, identity='test', path=entry['path']+'/text', compact=True)
    assert text['kind'] == 'string' and text['value'] and text['next_cursor']
