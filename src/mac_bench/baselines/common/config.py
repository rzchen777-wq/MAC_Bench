"""Resolve versioned method specifications without importing native runtimes."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import yaml

ROOT = __import__('mac_bench.runtime.paths', fromlist=['repository_root']).repository_root()
SPECS = ROOT / 'configs/baselines'


def redact_configuration(value):
    """Connection secrets can be supplied literally but must never be recorded."""
    if isinstance(value, dict):
        return {k: ('<redacted>' if k.lower() == 'api_key' and v else redact_configuration(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_configuration(v) for v in value]
    return value


def model_connection(method):
    from mac_bench.runtime.llm.config import LLMConfig
    mapping = dict(getattr(method, 'spec', {}).get('llm', {}))
    mapping.setdefault('max_retries', 0)
    config = LLMConfig.from_mapping(mapping)
    if config.max_retries:
        raise ValueError('Episode clients require llm.max_retries=0 for explicit attempt accounting')
    return config


def model_client(method):
    from mac_bench.runtime.llm.client import LiteLLMChatClient
    connection = model_connection(method)
    proxy = getattr(method, 'spec', {}).get('model_transport', {}).get('proxy')
    if proxy:
        import httpx
        from litellm.llms.custom_httpx.http_handler import HTTPHandler
        transport = HTTPHandler(client=httpx.Client(
            proxy=proxy, trust_env=False, timeout=connection.timeout_seconds))
        return LiteLLMChatClient(connection, publish_env=False, http_client=transport)
    return LiteLLMChatClient(connection, publish_env=False)


def tree_hash(root):
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(root)
    excluded = {'.git', '__pycache__', 'sessions', 'runs', 'save', 'node_modules'}
    files = {}
    for directory, directories, names in os.walk(root):
        directories[:] = [name for name in directories if name not in excluded]
        for name in names:
            path = Path(directory) / name
            if name not in excluded and path.is_file():
                files[str(path.relative_to(root))] = sha256(path.read_bytes()).hexdigest()
    return sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def load_spec(method, path=None):
    path = Path(path or SPECS / (method + '.yaml')).resolve()
    spec = yaml.safe_load(path.read_text())
    if spec['method'] != method or spec['implementation_status'] != 'implemented':
        raise ValueError('Method specification is not implemented: ' + method)
    reference = Path(spec['protocol_ref'])
    protocol_path = reference if reference.is_absolute() else path.parent / reference
    if not protocol_path.is_file() and not reference.is_absolute():
        protocol_path = ROOT / reference
    protocol = yaml.safe_load(protocol_path.read_text())
    if protocol['model']['homogeneous'] is not True:
        raise ValueError('A homogeneous backbone is required')
    spec['_protocol'] = protocol
    spec['_spec_path'] = str(path)
    root = ROOT / spec['source']['directory']
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != spec['source']['revision']:
        raise ValueError('Original source revision differs from method specification')
    return spec


def resolve(spec, context, *, assets=None):
    model = getattr(getattr(context.client, 'config', None), 'model', None)
    if not model:
        raise ValueError('An explicit experiment model is required')
    if any(isinstance(v,str) and v not in {'experiment.model',model}
           for v in spec['model_bindings'].values()):
        raise ValueError('All generative roles must use the selected backbone')
    if getattr(getattr(context.client, 'config', None), 'max_retries', 0):
        raise ValueError('Configure provider max_retries=0; hidden retries cannot be accounted')
    resolved = redact_configuration(deepcopy(spec))
    resolved['model_bindings'] = {key: model if value == 'experiment.model' else value
        for key, value in spec['model_bindings'].items()}
    resolved['model_parameters'] = spec['model_parameters']
    resolved['budget'] = vars(context.budget.limits)
    resolved['assets'] = assets or {}
    resolved['task_revision'] = context.task.world_revision
    connection = getattr(context.client, 'config', None)
    resolved['resolved_llm'] = {k: getattr(connection, k, None)
                               for k in ('base_url','model','timeout_seconds','max_retries','thinking_mode')}
    root = ROOT / spec['source']['directory']
    resolved['source_tree_hash'] = tree_hash(root)
    adapter = ROOT / 'src/mac_bench/baselines' / spec['method']
    resolved['adapter_hash'] = tree_hash(adapter)
    resolved['bridge_hash'] = tree_hash(ROOT / 'src/mac_bench/baselines/common')
    fingerprint = sha256(json.dumps(resolved, sort_keys=True).encode()).hexdigest()
    if context.artifacts is None:
        raise ValueError('Original methods require an episode artifact directory')
    directory = Path(context.artifacts).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {'method': spec['method'], 'variant': spec['variant'],
                'resolved_config_hash': fingerprint, 'configuration': resolved}
    (directory / 'method.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return directory
