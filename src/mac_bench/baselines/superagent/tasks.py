"""MAC-Bench adapter for the super-agent ticket machine."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import yaml
from mac_bench.baselines.common.config import ROOT, SPECS, redact_configuration, tree_hash
from . import runner


def load(config=None):
    path = Path(config or SPECS / 'superagent.yaml').resolve()
    spec = yaml.safe_load(path.read_text())
    if spec.get('method') != 'superagent' or spec.get('implementation_status') != 'implemented':
        raise ValueError('Method specification is not implemented: superagent')
    reference = Path(spec['protocol_ref'])
    protocol_path = reference if reference.is_absolute() else ROOT / reference
    protocol = yaml.safe_load(protocol_path.read_text())
    if protocol['model']['homogeneous'] is not True:
        raise ValueError('A homogeneous backbone is required')
    if spec.get('llm', {}).get('max_retries', 0):
        raise ValueError('Episode clients require llm.max_retries=0 for explicit attempt accounting')
    spec['_protocol'] = protocol
    spec['_spec_path'] = str(path)
    return spec


def record(spec, context):
    model = getattr(getattr(context.client, 'config', None), 'model', None)
    if not model:
        raise ValueError('An explicit experiment model is required')
    if any(isinstance(value, str) and value not in {'experiment.model', model}
           for value in spec['model_bindings'].values()):
        raise ValueError('All generative roles must use the selected backbone')
    if getattr(getattr(context.client, 'config', None), 'max_retries', 0):
        raise ValueError('Configure provider max_retries=0; hidden retries cannot be accounted')
    resolved = redact_configuration(deepcopy(spec))
    resolved['model_bindings'] = {key: model if value == 'experiment.model' else value
                                  for key, value in spec['model_bindings'].items()}
    resolved['model_parameters'] = spec['model_parameters']
    resolved['budget'] = vars(context.budget.limits)
    resolved['assets'] = {'dispatch': 'router_assigns_async_workers', 'workspace': 'shared_episode_world'}
    resolved['task_revision'] = context.task.world_revision
    connection = getattr(context.client, 'config', None)
    resolved['resolved_llm'] = {key: getattr(connection, key, None)
                                for key in ('base_url', 'model', 'timeout_seconds', 'max_retries', 'thinking_mode')}
    adapter = Path(__file__).resolve().parent
    resolved['adapter_hash'] = tree_hash(adapter)
    resolved['bridge_hash'] = tree_hash(adapter.parent / 'common')
    fingerprint = hashlib.sha256(json.dumps(resolved, sort_keys=True).encode()).hexdigest()
    if context.artifacts is None:
        raise ValueError('Episode methods require an episode artifact directory')
    directory = Path(context.artifacts).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {'method': spec['method'], 'variant': spec['variant'],
                'resolved_config_hash': fingerprint, 'configuration': resolved}
    (directory / 'method.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return directory


class Superagent:
    name = 'superagent'

    def __init__(self, config=None):
        self.spec = load(config)

    def validate(self):
        load(self.spec.get('_spec_path'))
        runner.runtime_dependencies()

    def run(self, context):
        record(self.spec, context)
        return runner.run(context, self.spec)
