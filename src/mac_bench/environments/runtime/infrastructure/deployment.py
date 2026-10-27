"""Read-only deployment evidence using the existing sandbox backend.

Records the running container's immutable image ID and explicitly selected
application source files. Tags and upstream documentation are not accepted as
substitutes for installed source. Does not start or modify any container.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath

from mac_bench.environments.errors import CompileFailure


@dataclass(frozen=True)
class DeploymentEvidence:
    sandbox_id: str
    container_id: str
    image_id: str
    image_reference: str
    sources: dict[str, dict]

    def to_dict(self):
        return {'sandbox_id': self.sandbox_id, 'container_id': self.container_id,
                'image_id': self.image_id, 'image_reference': self.image_reference,
                'sources': {key: dict(value) for key, value in self.sources.items()}}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {
                'sandbox_id', 'container_id', 'image_id', 'image_reference', 'sources'}
                or not isinstance(value['sandbox_id'], str) or not value['sandbox_id']
                or not isinstance(value['container_id'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['container_id'])
                or not isinstance(value['image_id'], str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['image_id'])
                or not isinstance(value['image_reference'], str)
                or not isinstance(value['sources'], dict) or not value['sources']):
            raise CompileFailure('deployment_evidence_invalid')
        for label, source in value['sources'].items():
            if (not isinstance(label, str) or not label or not isinstance(source, dict)
                    or set(source) != {'path', 'sha256', 'text'}
                    or not isinstance(source['path'], str)
                    or not PurePosixPath(source['path']).is_absolute()
                    or not isinstance(source['text'], str)
                    or sha256(source['text'].encode()).hexdigest() != source['sha256']):
                raise CompileFailure('deployment_source_invalid')
        return cls(value['sandbox_id'], value['container_id'], value['image_id'],
                   value['image_reference'], {key: dict(item) for key, item in value['sources'].items()})


def capture_deployment(backend, sandbox, *, container: str, source_paths: dict[str, str],
                       max_source_bytes: int = 2_000_000) -> DeploymentEvidence:
    """source_paths explicitly names code/schema files, never site config files.

    Capturing selected source establishes provenance, not completeness of all
    runtime hooks, plugins or database customization. Effective metadata must
    additionally be captured through the application's own API.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container) or not source_paths:
        raise CompileFailure('deployment_capture_configuration_invalid')

    def run(arguments):
        result = backend.exec(sandbox, shlex.join(arguments), timeout=30)
        if result.exit_code:
            raise CompileFailure('deployment_capture_command_failed')
        return result.stdout

    # Deliberately do not collect Config.Env or a full docker-inspect object.
    template = ('{"container_id":{{json .Id}},"image_id":{{json .Image}},'
                '"image_reference":{{json .Config.Image}},"running":{{json .State.Running}}}')

    def inspect():
        try:
            info = json.loads(run(['docker', 'container', 'inspect', '--format', template, container]))
        except (ValueError, TypeError):
            raise CompileFailure('deployment_inspection_invalid') from None
        if not isinstance(info, dict) or info.get('running') is not True:
            raise CompileFailure('deployment_container_not_running')
        return info

    before = inspect()
    sources = {}
    for label, path in sorted(source_paths.items()):
        if (not isinstance(path, str) or not PurePosixPath(path).is_absolute()
                or '..' in PurePosixPath(path).parts):
            raise CompileFailure('deployment_source_requires_absolute_path')
        text = run(['docker', 'exec', before['container_id'], 'cat', '--', path])
        if len(text.encode()) > max_source_bytes:
            raise CompileFailure('deployment_source_size_exceeded')
        sources[label] = {'path': path, 'sha256': sha256(text.encode()).hexdigest(), 'text': text}
    if inspect() != before:
        raise CompileFailure('deployment_changed_during_capture')
    return DeploymentEvidence.from_dict({
        'sandbox_id': sandbox.id, 'container_id': before['container_id'],
        'image_id': before['image_id'], 'image_reference': before['image_reference'],
        'sources': sources})


def capture_container_topology(backend, sandbox, *, containers: dict[str, str]) -> dict:
    """Observe explicitly selected service containers and their storage boundary.

    SaaS-Bench Pretix uses app/Postgres/Redis containers and separate volumes;
    other apps may keep DBs inside a bundled container's writable layer. Neither
    a container image nor one app directory is a complete world snapshot.

    No Env, command line, arbitrary labels or credentials are collected. This
    function does not start/stop services or prove that the selected service set
    includes every dependency. Shared mounts are reported, not deemed exclusive.
    """
    if (not isinstance(containers, dict) or not containers
            or any(not isinstance(role, str) or not role or not isinstance(name, str)
                   or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name)
                   for role, name in containers.items())):
        raise CompileFailure('deployment_topology_selection_invalid')
    template = ('{"container_id":{{json .Id}},"image_id":{{json .Image}},'
                '"image_reference":{{json .Config.Image}},"name":{{json .Name}},'
                '"running":{{json .State.Running}},"mounts":{{json .Mounts}},'
                '"ports":{{json .NetworkSettings.Ports}}}')

    def inspect():
        observed, identities = {}, set()
        for role, name in sorted(containers.items()):
            command = shlex.join(['docker', 'container', 'inspect', '--format', template, name])
            result = backend.exec(sandbox, command, timeout=30)
            if result.exit_code:
                raise CompileFailure('deployment_topology_inspection_failed')
            try:
                value = json.loads(result.stdout)
            except (ValueError, TypeError):
                raise CompileFailure('deployment_topology_response_invalid') from None
            if (not isinstance(value, dict) or value.get('running') is not True
                    or not isinstance(value.get('container_id'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', value['container_id'])
                    or not isinstance(value.get('image_id'), str)
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['image_id'])
                    or not isinstance(value.get('mounts'), list)
                    or not isinstance(value.get('ports'), dict)):
                raise CompileFailure('deployment_topology_container_invalid')
            if value['container_id'] in identities:
                raise CompileFailure('deployment_topology_duplicate_container')
            identities.add(value['container_id'])
            mounts = []
            for mount in value['mounts']:
                if (not isinstance(mount, dict) or mount.get('Type') not in {'volume', 'bind', 'tmpfs'}
                        or not isinstance(mount.get('Destination'), str)
                        or not PurePosixPath(mount['Destination']).is_absolute()
                        or type(mount.get('RW')) is not bool):
                    raise CompileFailure('deployment_topology_mount_invalid')
                # Preserve exact volume/source identity. Do not infer that data
                # outside these destinations is ephemeral or safe to discard.
                mounts.append({key: mount.get(key) for key in ('Type', 'Name', 'Source', 'Destination', 'RW')})
            observed[role] = {**value, 'mounts': sorted(mounts, key=lambda item: item['Destination'])}
        return observed

    before = inspect()
    if inspect() != before:
        raise CompileFailure('deployment_topology_changed_during_capture')
    return {'version': 1, 'sandbox_id': sandbox.id, 'containers': before,
            'coverage': 'explicitly selected containers only',
            'writable_layers_captured': False, 'volume_contents_captured': False}
