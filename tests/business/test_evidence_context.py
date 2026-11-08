"""Evidence remains interpretable after projection, pagination and SQL joins."""
import json
from types import SimpleNamespace

import pytest

import gzip
from hashlib import sha256
from pathlib import Path
from mac_bench.environments.tools.contracts import encoded
from mac_bench.environments.tools.data_workspace import execute_sql
from mac_bench.environments.tools.evidence import EvidenceContext
from mac_bench.environments.tools.navigation import PAGE_BYTES, page, resolve
from mac_bench.environments.tools.query import query_document
from mac_bench.evaluation.tasks.resources import PublicResources


def extract(text):
    return json.loads(gzip.decompress((Path(__file__).parent/'fixtures/evidence.json.gz').read_bytes()))[sha256(text.encode()).hexdigest()]


SOURCE = '''# Terms
**Currency**: USD
## Case A
**Accepted**: Yes.
| Item | Price |
| --- | --- |
| 001 | 80 |
| 002 | 40 |
The two components have separate prices.
**Authorized**: Yes.
## Case B
**Accepted**: No.
| Item | Price |
| --- | --- |
| 003 | 60 |
'''


def test_projected_rows_preserve_common_conditions_without_filling_answers():
    doc = extract(SOURCE)
    result = query_document(doc, identity='test', path='/collections/0/rows',
        fields={'item': '/item', 'price': '/price'}, context_provider=EvidenceContext(doc))
    assert [r['values'] for r in result['rows']] == [
        {'item': '001', 'price': 80}, {'item': '002', 'price': 40}, {'item': '003', 'price': 60}]
    a, b = [result['contexts'][result['rows'][i]['context']] for i in (0, 2)]
    assert a['section_path'] != b['section_path']
    assert [(v['field'], v['value']) for v in a['fields']] == [
        ('Accepted', 'Yes.'), ('Authorized', 'Yes.')]
    assert b['fields'][0]['value'] == 'No.'
    assert a['parent']['labelled_fact_count'] == 1
    source = a['source']
    assert SOURCE[source['offset']:source['offset']+source['length']].startswith('## Case A')
    assert '## Case B' not in SOURCE[source['offset']:source['offset']+source['length']]
    assert len(result['contexts']) == 2
    assert 'accepted' not in doc['tables'][0]['rows'][0]


def test_section_navigation_exposes_parent_and_full_label_paths():
    doc = extract(SOURCE)
    provider = EvidenceContext(doc)
    identity, detail = provider(doc['tables'][0], '/tables/0')
    result = page(doc, identity='test', path=detail['parent']['section_path'], context_provider=provider)
    parent_context = next(iter(result['contexts'].values()))
    assert parent_context['fields'][0]['value'] == 'USD'
    leaf = page(doc, identity='test', path='/tables/0/rows/0/price', context_provider=provider)
    assert leaf['value'] == 80 and leaf['context'] == identity


@pytest.mark.parametrize('method', ['read', 'query'])
def test_many_section_pages_are_bounded_and_recover_every_row(method):
    text = ''.join(f'## Case {i}\n**Condition**: {"X" * 200}\n'
        f'| ID | Amount |\n| --- | --- |\n| {i} | {i+1} |\n' for i in range(90))
    doc = extract(text)
    provider = EvidenceContext(doc)
    cursor, found, pages = None, [], 0
    while True:
        if method == 'query':
            result = query_document(doc, identity='test', path='/collections/0/rows',
                fields={'id': '/id'}, limit=100, cursor=cursor, context_provider=provider)
            rows = result['rows']
            found.extend(r['values']['id'] for r in rows)
        else:
            result = page(doc, identity='test', path='/collections/0/rows', compact=True,
                limit=100, cursor=cursor, context_provider=provider)
            rows = result['items']
            found.extend(r['value']['id'] for r in rows)
        assert len(encoded(result).encode()) <= PAGE_BYTES
        assert rows
        assert set(r['context'] for r in rows) == set(result['contexts'])
        cursor = result['next_cursor']
        pages += 1
        if not cursor:
            break
        assert pages <= 90
    assert found == list(range(90))
    assert pages > 1


def test_long_section_context_and_strings_remain_recoverable():
    text = '# Case\n' + ''.join(f'**Field {i}**: {"资格" * 3000}\n' for i in range(30))
    text += '| ID |\n| --- |\n| X |\n'
    doc = extract(text)
    provider = EvidenceContext(doc)
    result = query_document(doc, identity='test', path='/tables/0/rows', context_provider=provider)
    detail = next(iter(result['contexts'].values()))
    assert not detail['fields_complete']
    assert len(encoded(detail).encode()) <= 2000
    assert not detail['fields'][0]['value']['complete']
    cursor, parts = None, []
    while True:
        chunk = page(doc, identity='test', path=detail['fields'][0]['value']['path'],
                     compact=True, cursor=cursor, context_provider=provider)
        assert len(encoded(chunk).encode()) <= PAGE_BYTES
        parts.append(chunk['value'])
        cursor = chunk['next_cursor']
        if not cursor:
            break
    assert ''.join(parts) == '资格' * 3000
    all_fields = query_document(doc, identity='test', **detail['fields_query'],
                               aggregates=[{'name': 'n', 'op': 'count'}])
    assert all_fields['aggregates']['n']['value'] == 30




def test_compute_can_join_source_statements_to_rows_without_private_information():
    doc = extract(SOURCE)
    result = execute_sql({'items': doc['collections'][0]['rows'], 'statements': doc['fields']},
        'SELECT i.item,s.raw_value AS accepted FROM items i JOIN statements s '
        'ON i._section_id=s.section_id WHERE s.field=? ORDER BY i.item', parameters=['Accepted'])
    assert result['rows'] == [{'item': '001', 'accepted': 'Yes.'},
        {'item': '002', 'accepted': 'Yes.'}, {'item': '003', 'accepted': 'No.'}]


def test_public_resource_entry_points_preserve_context(tmp_path):
    (tmp_path / 'public').mkdir()
    (tmp_path / 'public/facts.json').write_text(json.dumps(extract(SOURCE)))
    bundle = SimpleNamespace(root=tmp_path, manifest={'revision': 'test',
        'public_files': ['public/facts.json']})
    resources = PublicResources(bundle)
    result = resources.query('public/facts.json', path='/tables/0/rows', fields={'item': '/item'})
    context = result['contexts'][result['rows'][0]['context']]
    assert context['fields'][0]['value'] == 'Yes.'
    section = resources.read('public/facts.json', path=context['section_path'])
    assert 'contexts' in section
