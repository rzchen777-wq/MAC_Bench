"""Validate a service group's explicit infrastructure contract offline."""
import re
from pathlib import PurePosixPath

from .engine import (
    SnapshotError,
)


def service_order(spec):
    """Resolve explicit dependencies; legacy contracts retain their supplied order."""
    names = spec['containers']
    dependencies = spec.get('dependencies')
    if dependencies is None:
        return list(names)
    if (not isinstance(dependencies, dict) or set(dependencies) != set(names)
            or any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                   or len(items) != len(set(items)) or any(item not in names or item == name for item in items)
                   for name, items in dependencies.items())):
        raise SnapshotError('container_snapshot_dependencies_invalid')
    ordered, remaining = [], set(names)
    while remaining:
        ready = [name for name in names if name in remaining
                 and not remaining.intersection(dependencies[name])]
        if not ready:
            raise SnapshotError('container_snapshot_dependency_cycle')
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def validate(spec, store):
    required = {'containers', 'owned_bind_roots', 'readiness', 'timeout_seconds',
                'stop_timeout_seconds', 'readiness_timeout_seconds', 'deployments'}
    if not isinstance(spec, dict) or not required <= set(spec) or set(spec) - required - {'dependencies'}:
        raise SnapshotError('container_snapshot_configuration_schema')
    names = spec['containers']
    if (not isinstance(names, list) or not names or len(set(names)) != len(names)
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name)
                   for name in names)):
        raise SnapshotError('container_snapshot_requires_ordered_unique_service_names')
    service_order(spec)
    roots = spec['owned_bind_roots']
    if (not isinstance(roots, list) or len(set(roots)) != len(roots)
            or any(not isinstance(path, str) or not PurePosixPath(path).is_absolute()
                   or '..' in PurePosixPath(path).parts for path in [*roots, store])):
        raise SnapshotError('container_snapshot_requires_absolute_owned_paths')
    if any(type(spec[key]) is not int or spec[key] < 1 for key in
           ('timeout_seconds', 'stop_timeout_seconds', 'readiness_timeout_seconds')):
        raise SnapshotError('container_snapshot_timeouts_invalid')
    if spec['stop_timeout_seconds'] >= spec['timeout_seconds']:
        raise SnapshotError('container_snapshot_stop_timeout_exceeds_operation_timeout')
    if not isinstance(spec['deployments'], dict) or not spec['deployments']:
        raise SnapshotError('container_snapshot_captured_deployments_required')
    for deployment in spec['deployments'].values():
        if (not isinstance(deployment, dict) or set(deployment) != {'container_id', 'image_id', 'sources'}
                or not isinstance(deployment['container_id'], str)
                or not re.fullmatch('[0-9a-f]{64}', deployment['container_id'])
                or not isinstance(deployment['image_id'], str)
                or not re.fullmatch('sha256:[0-9a-f]{64}', deployment['image_id'])
                or not isinstance(deployment['sources'], dict) or not deployment['sources']):
            raise SnapshotError('container_snapshot_deployment_contract_invalid')
        for path, checksum in deployment['sources'].items():
            if (not isinstance(path, str) or not PurePosixPath(path).is_absolute()
                    or '..' in PurePosixPath(path).parts or not isinstance(checksum, str)
                    or not re.fullmatch('[0-9a-f]{64}', checksum)):
                raise SnapshotError('container_snapshot_source_contract_invalid')
    readiness = spec['readiness']
    if not isinstance(readiness, dict) or set(readiness) != set(names):
        raise SnapshotError('container_snapshot_requires_each_service_readiness')
    for probe in readiness.values():
        if not isinstance(probe, dict) or probe.get('kind') not in {'http', 'tcp', 'exec'}:
            raise SnapshotError('container_snapshot_readiness_probe_schema')
        if probe['kind'] == 'exec':
            if (set(probe) != {'kind', 'argv'} or not isinstance(probe['argv'], list)
                    or not probe['argv'] or any(not isinstance(arg, str) or not arg or '\x00' in arg
                                              for arg in probe['argv'])):
                raise SnapshotError('container_snapshot_readiness_command_invalid')
            continue
        keys = {'kind', 'host', 'port'} | ({'path', 'statuses'} if probe['kind'] == 'http' else set())
        if (not keys <= set(probe) or set(probe) - keys - ({'headers'} if probe['kind'] == 'http' else set())
                or not isinstance(probe['host'], str) or not probe['host']
                or type(probe['port']) is not int or not 1 <= probe['port'] <= 65535):
            raise SnapshotError('container_snapshot_readiness_address_invalid')
        if probe['kind'] == 'http' and (
                not isinstance(probe['path'], str) or not probe['path'].startswith('/')
                or not isinstance(probe['statuses'], list) or not probe['statuses']
                or any(type(status) is not int or not 100 <= status <= 599 for status in probe['statuses'])):
            raise SnapshotError('container_snapshot_readiness_HTTP_contract_invalid')
        if 'headers' in probe and (
                not isinstance(probe['headers'], dict)
                or any(not isinstance(key, str) or not key or not isinstance(value, str)
                       or any(char in key + value for char in '\r\n')
                       for key, value in probe['headers'].items())):
            raise SnapshotError('container_snapshot_readiness_headers_invalid')
