"""Native adapters must not turn truncated actions into executable responses."""
from types import SimpleNamespace
import pytest
from mac_bench.baselines.common.gateway import Gateway, MethodStop
from mac_bench.runtime.llm.client import ChatResponse, ModelOutputTruncatedError


def gateway(values):
    calls, events = [], []
    values = iter(values)
    def complete(messages, **options):
        calls.append((messages, options))
        result = next(values)
        if isinstance(result, Exception):
            raise result
        return result
    ctx = SimpleNamespace(client=SimpleNamespace(complete=complete), event=events.append)
    return Gateway(ctx, {'temperature': .2, 'max_tokens': 8192}), calls, events


def truncated():
    return ModelOutputTruncatedError('length', response=ChatResponse('draft', model='test', usage={},
        tool_calls=({'id':'partial','type':'function',
                    'function':{'name':'write','arguments':'{}'}},)))


def test_recovery_regenerates_original_request_and_withholds_partial_actions():
    done = ChatResponse('complete', model='test', usage={})
    g, calls, events = gateway([truncated(), truncated(), done])
    original = [{'role':'user','content':'Use the native action protocol.'}]
    prepared = []
    assert g.complete(original, prepare_context=prepared.append) is done
    assert [o['max_tokens'] for _, o in calls] == prepared == [8192, 16384, 32768]
    assert all(m[0].content == original[0]['content'] and len(m) == 1 for m, _ in calls)
    assert len([e for e in events if e['kind']=='completion_recovery']) == 2


def test_summary_allowance_is_respected_and_failure_is_not_hidden():
    g, calls, _ = gateway([truncated()])
    with pytest.raises(MethodStop) as stopped:
        g.complete([{'role':'user','content':'Summarize evidence.'}],
                   max_tokens=24576, recover_truncation=False)
    assert isinstance(stopped.value.error, ModelOutputTruncatedError)
    assert len(calls) == 1 and calls[0][1]['max_tokens'] == 24576


def test_action_recovery_requests_one_action_without_replaying_truncated_draft():
    done = ChatResponse('complete', model='test', usage={})
    g, calls, _ = gateway([truncated(), done])
    g.parameters['recovery_instruction'] = 'Return one JSON action, then stop.'
    original = [{'role': 'user', 'content': 'Task'}]
    assert g.complete(original) is done
    assert len(original) == 1
    assert 'one JSON action' in calls[1][0][-1].content
    assert 'draft' not in str(calls[1][0])


def test_recovery_is_bounded_and_does_not_retry_other_failures():
    for values, expected_calls in [([truncated()]*3, 3), ([RuntimeError('provider')], 1)]:
        g, calls, _ = gateway(values)
        with pytest.raises(MethodStop):
            g.complete([{'role':'user','content':'Task'}])
        assert len(calls) == expected_calls


def test_coder_text_continuation_is_assembled_only_after_finish():
    interrupted = ModelOutputTruncatedError('length', response=ChatResponse(
        '```python\nprint(', model='test', usage={}, finish_reason='length'))
    tail = ChatResponse('"done")\n```', model='test', usage={})
    g, calls, events = gateway([interrupted, tail])
    prepared = []
    response = g.complete([{'role': 'user', 'content': 'Write code'}], continue_text=True,
                         prepare_context_batch=lambda cap, batch: prepared.append(list(batch)))
    assert response.content == '```python\nprint("done")\n```'
    assert len(calls) == len(prepared) == 2
    assert prepared[-1][-2].content == '```python\nprint('
    assert not response.tool_calls
    assert events[-1]['kind'] == 'method_model_end'
    assert any(e.get('strategy') == 'continue_unexecuted_text' for e in events)


def test_native_history_preserves_compaction_and_appends_new_tool_turns():
    from mac_bench.baselines.common.history import NativeHistory
    from mac_bench.runtime.agents.context import ContextPolicy
    from mac_bench.runtime.llm.client import ChatMessage
    g, calls, events = gateway([ChatResponse('Observed write succeeded; recover details at /tmp/log.',
                                            model='test', usage={})] * 10)
    h = NativeHistory(g, ContextPolicy(request_token_limit=4096, target_input_tokens=2000,
        summary_output_tokens=256, recent_turns=1, recent_tokens=500))
    source = [ChatMessage('system','Native policy'),ChatMessage('user','Public task')]
    for i in range(4):
        source.extend([ChatMessage('assistant', tool_calls=({'id':str(i),'type':'function',
            'function':{'name':'write','arguments':'{}'}},)),
            ChatMessage('tool', 'mutation receipt '+str(i)+' x'*2000, tool_call_id=str(i))])
    h.sync(source)
    h.prepare((), 512)
    assert any(e['kind']=='context_compaction' for e in events)
    memory = h.messages[2]
    assert 'Observed write succeeded' in memory.content
    source.extend([ChatMessage('assistant','Continue work'),ChatMessage('user','New message')])
    view = h.sync(source)
    assert view[2] is memory and view[-1].content == 'New message'


def test_delegation_report_pairs_outcomes_and_keeps_pagination_and_finish():
    from mac_bench.baselines.aorchestra.trajectory import report_messages
    read = SimpleNamespace(action={'action':'read_resource','params':{'offset':8000}},
        observation={'text':'PREVIOUS PAGE'},reward=0,
        info={'last_action_result':{'text':'LAST PAGE '*1000,'next_offset':None}})
    finish = SimpleNamespace(action={'action':'finish','params':{'status':'done','result':'Read complete'}},
        observation={'text':'LAST PAGE'},reward=0,
        info={'finished':True,'finish_result':{'status':'done','result':'Read complete'}})
    messages = report_messages([read,finish], 'Read all requirements.')
    assert 'PREVIOUS PAGE' not in messages[3].content
    assert 'next_offset' in messages[3].content and 'null' in messages[3].content
    assert messages[3].content.count('LAST PAGE')==1000
    assert 'Read complete' in messages[-1].content
