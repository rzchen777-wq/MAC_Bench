"""Selectable model connections, credential isolation and embedding compatibility."""
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
import yaml
from mac_bench.baselines.common.config import SPECS, model_client, resolve
from mac_bench.baselines.common.registry import create_method
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.runtime.llm.embedding import EmbeddingConfig, LiteLLMEmbeddingClient


def test_pool_hash_covers_assets_under_runs_and_survives_relocation(tmp_path):
    import shutil
    from mac_bench.baselines.common.config import tree_hash
    source = tmp_path / 'assets'
    source.mkdir()
    (source / 'prompt.md').write_text('Original behavior')
    ignored = source / 'runs'
    ignored.mkdir()
    (ignored / 'session.json').write_text('Ephemeral runtime state')
    relocated = tmp_path / 'runs' / 'team' / 'v001'
    shutil.copytree(source, relocated)
    expected = tree_hash(source)
    assert tree_hash(relocated) == expected
    (relocated / 'prompt.md').write_text('Changed behavior')
    assert tree_hash(relocated) != expected


@pytest.mark.parametrize('name',['react_single','aorchestra','meta_team','magentic_one'])
@pytest.mark.baselines
def test_each_method_selects_its_litellm_connection_and_redacts_manifest(tmp_path,name,monkeypatch):
    spec=yaml.safe_load((SPECS/(name+'.yaml')).read_text())
    spec['llm'].update(base_url='http://model.example/v1',model_name='selected-model',
                       api_key='synthetic-secret',max_retries=0)
    path=tmp_path/(name+'.yaml');path.write_text(yaml.safe_dump(spec))
    monkeypatch.setenv('MAC_BENCH_LLM_MODEL','wrong-global-model')
    method=create_method(name,config=path);client=model_client(method)
    assert client.config.base_url=='http://model.example/v1' and client.config.model=='selected-model'
    assert client.config.api_key=='synthetic-secret'
    assert client.config.max_retries==0
    if name!='react_single':
        context=SimpleNamespace(client=client,budget=SimpleNamespace(limits=AgentBudget()),
            task=SimpleNamespace(world_revision='revision'),artifacts=tmp_path/'native')
        directory=resolve(method.spec,context)
        text=(directory/'method.json').read_text()
        assert 'synthetic-secret' not in text and '<redacted>' in text
        assert json.loads(text)['configuration']['resolved_llm']['model']=='selected-model'


def test_embedding_has_independent_credentials_and_litellm_batch_order(monkeypatch):
    monkeypatch.setenv('MAC_BENCH_LLM_API_KEY','chat-secret')
    monkeypatch.setenv('EMBED_KEY','embedding-secret')
    config=EmbeddingConfig.from_mapping({'base_url':'http://embedding.example/v1',
        'model_name':'custom-vector-model','api_key':'${EMBED_KEY}','batch_size':2},environ=__import__('os').environ)
    calls=[];records=[]
    def request(**kwargs):
        calls.append(kwargs)
        return {'data':[{'index':i,'embedding':[i+1,2,3]} for i in reversed(range(len(kwargs['input'])))],
                'usage':{'prompt_tokens':len(kwargs['input']),'total_tokens':len(kwargs['input'])}}
    from mac_bench.runtime.llm import gateway
    monkeypatch.setattr(gateway.litellm,'embedding',request)
    client=LiteLLMEmbeddingClient(config,record=records.append)
    result=client.encode(['a','b','c'],batch_size=512)
    assert result.tolist()==[[1,2,3],[2,2,3],[1,2,3]]
    assert all(c['api_key']=='embedding-secret' and c['model']=='openai/custom-vector-model'
               and c['api_base']==config.base_url and c['num_retries']==0 for c in calls)
    assert [len(c['input']) for c in calls]==[2,1]
    assert 'embedding-secret' not in json.dumps(records) and 'chat-secret' not in repr(config)
    assert client.encode('single').shape==(3,)


def test_embedding_does_not_fall_back_to_chat_credentials():
    config=EmbeddingConfig.from_mapping({'model_name':'embed'},environ={'MAC_BENCH_LLM_API_KEY':'chat'})
    assert config.api_key is None
    with pytest.raises(ValueError,match='model_name'):
        EmbeddingConfig.from_mapping({},environ={})


def test_embedding_dimension_drift_is_visible(monkeypatch):
    from mac_bench.runtime.llm import gateway
    outputs=iter([[1,2,3],[1,2]])
    monkeypatch.setattr(gateway,'embed',lambda **kw:{'data':[{'index':0,'embedding':next(outputs)}]})
    client=LiteLLMEmbeddingClient(EmbeddingConfig('http://embed','vector',batch_size=1))
    with pytest.raises(ValueError,match='dimensions'):
        client.encode(['a','b'])



def test_embedding_uses_current_configuration_namespace():
    config = EmbeddingConfig.from_mapping({}, environ={
        'MAC_BENCH_EMBEDDING_BASE_URL': 'https://embedding.example/v1',
        'MAC_BENCH_EMBEDDING_MODEL': 'vector',
        'MAC_BENCH_EMBEDDING_API_KEY': 'fixture-key'})
    assert config.base_url == 'https://embedding.example/v1'
    assert config.model_name == 'vector' and config.api_key == 'fixture-key'
