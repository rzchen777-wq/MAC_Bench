"""Owner-supplied dependency infrastructure, without editing sealed task bundles."""
import json
import os
from hashlib import sha256
from pathlib import Path

ENVIRONMENT_VARIABLE = 'EVOTEAM_ENGINEERING_INFRASTRUCTURE_OVERRIDES'


def load_override(bundle, environment):
    locator = os.environ.get(ENVIRONMENT_VARIABLE)
    if not locator:
        return None
    path = Path(locator)
    content = path.read_bytes()
    configuration = json.loads(content)
    if configuration.get('format') != 'collaboration.dependency_infrastructure.v1':
        raise ValueError('Unsupported dependency infrastructure configuration')
    entry = configuration['worlds'].get(bundle.manifest['world_id'])
    if entry is None:
        return None
    if entry['world_revision'] != bundle.manifest['revision']:
        raise ValueError('Dependency infrastructure belongs to a different sealed revision')
    original = bundle.manifest['metadata']['initial_snapshot']
    if entry['original_snapshot'] != original or entry.get('source_free') is not True:
        raise ValueError('Dependency infrastructure must replace the declared snapshot without source')
    expected_build = environment['build'].replace('yarn install ', 'yarn install --offline ').replace(
        'npm ci', 'npm ci --offline --no-audit --no-fund')
    if entry['build'] != expected_build:
        raise ValueError('Dependency infrastructure may only enable offline dependency installation')
    if not Path(entry['provider_directory']).is_dir():
        raise ValueError('Dependency snapshot provider receipts are unavailable')
    return {
        **entry, 'configuration_path': str(path.resolve()),
        'configuration_sha256': sha256(content).hexdigest(),
        'candidate_network_blocked': True, 'grader_network_blocked': True,
    }
