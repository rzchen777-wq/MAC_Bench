"""Private service-group snapshot state machine inside an owned Modal VM.

No business RPC is replayed. Failures leave the group and a persistent operation
journal unresolved. Only an entirely restored group may resume. Kernel/process
memory is not checkpointed; native business readback remains the final reset gate.
"""
import json
import re
import socket
import subprocess
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import quote

from .engine import (
    Engine,
    SnapshotError,
    changed_host_fields,
    configuration,
    create_body,
    mount_signature,
)
from .storage import (
    archive,
    capture_mount,
    check_tree,
    digest_file,
    overlaps,
    restore_mount,
    save_private,
    verify_mount,
)
from .configuration import service_order


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ContainerSnapshots:
    def __init__(self, spec, store):
        self.spec = spec
        self.store = Path(store).resolve()
        self.state_path = self.store / 'state.json'
        self.names = service_order(spec)
        self.engine = Engine(spec['timeout_seconds'])
        self.timeout = spec['timeout_seconds']
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {'phase': 'new'}
        if self.state.get('spec_digest', digest(spec)) != digest(spec):
            raise SnapshotError('container_snapshot_configuration_changed')

    def save(self, phase=None, **updates):
        if phase is not None:
            self.state['phase'] = phase
        self.state.update(updates)
        self.state['spec_digest'] = digest(self.spec)
        save_private(self.state_path, self.state)

    def require_phase(self, phases):
        if self.state['phase'] not in phases:
            raise SnapshotError('container_snapshot_operation_out_of_order')

    def preflight(self):
        self.require_phase({'new'})
        containers = {name: self.engine.inspect(name) for name in self.names}
        mounts = self.topology(containers)
        if any(not info['State']['Running'] or info['State']['Paused'] for info in containers.values()):
            raise SnapshotError('container_snapshot_services_not_running')
        self.check_deployments(containers)
        # Resolve creation configurations now, before baseline business writes.
        # This also rejects implicit mount modes that cannot be faithfully bound.
        for info in containers.values():
            create_body(info, info['Image'])
        return {'containers': {name: info['Id'] for name, info in containers.items()},
                'mount_count': len(mounts), 'container_layers_included': True,
                'mount_storage_included': True, 'native_reset_verified': False}

    def check_deployments(self, containers):
        by_id = {info['Id']: info for info in containers.values()}
        for deployment in self.spec['deployments'].values():
            actual = by_id.get(deployment['container_id'])
            if actual is None or actual['Image'] != deployment['image_id']:
                raise SnapshotError('container_snapshot_captured_application_not_in_service_group')
            for path, checksum in deployment['sources'].items():
                result = subprocess.run(['docker', 'exec', actual['Id'], 'sha256sum', '--', path],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=self.timeout)
                observed = result.stdout.split(maxsplit=1)
                if result.returncode or not observed or observed[0].decode() != checksum:
                    raise SnapshotError('container_snapshot_deployed_source_changed')

    def topology(self, containers):
        mounts = {}
        allowed = {str(Path(path).resolve()) for path in self.spec['owned_bind_roots']}
        docker_root = self.engine.call('GET', '/info')['DockerRootDir']
        for name, info in containers.items():
            if info['Name'] != '/' + name:
                raise SnapshotError('container_snapshot_service_name_changed')
            host = info['HostConfig']
            if (host.get('AutoRemove') or host.get('VolumesFrom') or host.get('Tmpfs')
                    or host.get('ContainerIDFile') or host.get('Devices')
                    or host.get('Privileged') or info['Config'].get('NetworkDisabled')
                    or any(str(host.get(key, '')).startswith('container:')
                           for key in ('NetworkMode', 'PidMode', 'IpcMode', 'UTSMode'))
                    or host.get('NetworkMode') in {'host', 'none'}):
                raise SnapshotError('container_snapshot_external_or_ephemeral_resource_not_supported')
            if not info['NetworkSettings']['Networks']:
                raise SnapshotError('container_snapshot_service_network_missing')
            for mount in info['Mounts']:
                kind = mount['Type']
                if kind not in {'bind', 'volume'}:
                    raise SnapshotError('container_snapshot_mount_type_not_supported')
                source = str(Path(mount['Source']).resolve())
                if kind == 'bind' and source not in allowed:
                    raise SnapshotError('container_snapshot_bind_storage_not_explicitly_owned')
                if kind == 'bind' and overlaps(source, docker_root):
                    raise SnapshotError('container_snapshot_bind_overlaps_engine_storage')
                if kind == 'volume':
                    volume = self.engine.call('GET', '/volumes/' + quote(mount['Name'], safe=''))
                    if (volume.get('Driver') != 'local' or volume.get('Options')
                            or str(Path(volume['Mountpoint']).resolve()) != source):
                        raise SnapshotError('container_snapshot_volume_driver_not_local_storage')
                if overlaps(source, self.store):
                    raise SnapshotError('container_snapshot_store_overlaps_application_storage')
                if any(source != existing and overlaps(source, existing) for existing in mounts):
                    raise SnapshotError('container_snapshot_overlapping_mount_sources')
                key = sha256(source.encode()).hexdigest()
                entry = mounts.setdefault(source, {'key': key, 'source': source, 'writable': False})
                entry['writable'] |= bool(mount['RW'])
                check_tree(source)
        # A volume shared with any outside service is not owned by this group,
        # even if the other service happens to be stopped at the moment.
        selected = {info['Id'] for info in containers.values()}
        for other in self.engine.call('GET', '/containers/json', query={'all': 'true'}):
            if other['Id'] in selected:
                continue
            for mount in other.get('Mounts', []):
                if mount.get('Source') and any(overlaps(mount['Source'], source) for source in mounts):
                    raise SnapshotError('container_snapshot_storage_has_external_consumer')
        return mounts

    def quiesce(self):
        self.require_phase({'new', 'resumed'})
        before = {name: self.engine.inspect(name) for name in self.names}
        if any(not info['State']['Running'] or info['State']['Paused'] or info['State']['Restarting']
               for info in before.values()):
            raise SnapshotError('container_snapshot_services_not_running')
        prior = self.state.get('current_ids')
        if prior is not None and prior != {name: info['Id'] for name, info in before.items()}:
            raise SnapshotError('container_snapshot_service_replaced_outside_checkpoint')
        if prior is None:
            self.check_deployments(before)
        mounts = self.topology(before)
        self.save('quiescing', before=before, mounts=mounts, stopped=[], pending=None)
        for name in reversed(self.names):
            self.save(pending={'operation': 'stop', 'container': name})
            self.engine.call('POST', '/containers/' + before[name]['Id'] + '/stop',
                             query={'t': self.spec['stop_timeout_seconds']})
            after = self.engine.stopped(name)
            if after['Id'] != before[name]['Id'] or configuration(after) != configuration(before[name]):
                raise SnapshotError('container_snapshot_configuration_changed_during_stop')
            if after['State'].get('OOMKilled') or after['State'].get('ExitCode') == 137:
                raise SnapshotError('container_snapshot_forced_service_shutdown')
            if after['State'].get('ExitCode') not in (0, 143):
                raise SnapshotError('container_snapshot_service_shutdown_failed')
            self.save(stopped=[*self.state['stopped'], name], pending=None)
        self.save('quiesced')
        return {'stopped_containers': self.names}

    def assert_stopped(self):
        values = {name: self.engine.stopped(name) for name in self.names}
        expected = self.state.get('current_ids') if self.state['phase'] == 'restored' else None
        if expected is None:
            expected = {name: info['Id'] for name, info in self.state['before'].items()}
        if expected != {name: info['Id'] for name, info in values.items()}:
            raise SnapshotError('container_snapshot_stopped_group_changed')
        return values

    def capture(self):
        self.require_phase({'quiesced'})
        self.assert_stopped()
        identity = uuid.uuid4().hex
        directory = self.store / identity
        directory.mkdir(mode=0o700)
        self.save('capturing', snapshot_id=identity)
        manifest = {'spec_digest': digest(self.spec), 'containers': deepcopy(self.state['before']),
                    'mounts': deepcopy(self.state['mounts']), 'images': {}, 'mount_receipts': {}}
        for name in self.names:
            self.save(pending={'operation': 'commit', 'container': name})
            result = self.engine.call('POST', '/commit', query={
                'container': manifest['containers'][name]['Id'], 'pause': 'false',
                'comment': 'EvoTeam quiesced application checkpoint'})
            image = self.engine.image(result['Id'])
            manifest['images'][name] = {'id': image['Id'], 'layers': image['RootFS']['Layers']}
            save_private(directory / 'partial.json', manifest)
            self.save(pending=None)
        for source, mount in manifest['mounts'].items():
            child = directory / mount['key']
            child.mkdir(mode=0o700)
            self.save(pending={'operation': 'archive_mount', 'key': mount['key']})
            manifest['mount_receipts'][source] = capture_mount(source, child, self.timeout)
            self.save(pending=None)
        self.assert_stopped()
        checksum = save_private(directory / 'manifest.json', manifest)
        token = {'snapshot_id': identity, 'manifest_digest': checksum}
        self.save('captured', resume_containers=manifest['containers'], token=token)
        return token

    def verified(self, token):
        if (not isinstance(token, dict) or set(token) != {'snapshot_id', 'manifest_digest'}
                or not isinstance(token['snapshot_id'], str)
                or not re.fullmatch('[0-9a-f]{32}', token['snapshot_id'])):
            raise SnapshotError('container_snapshot_token_invalid')
        directory = self.store / token['snapshot_id']
        if digest_file(directory / 'manifest.json') != token['manifest_digest']:
            raise SnapshotError('container_snapshot_manifest_changed')
        manifest = json.loads((directory / 'manifest.json').read_text())
        if manifest['spec_digest'] != digest(self.spec) or set(manifest['containers']) != set(self.names):
            raise SnapshotError('container_snapshot_restore_scope_changed')
        if (set(manifest['images']) != set(self.names)
                or set(manifest['mount_receipts']) != set(manifest['mounts'])):
            raise SnapshotError('container_snapshot_snapshot_set_incomplete')
        for image in manifest['images'].values():
            actual = self.engine.image(image['id'])
            if actual['Id'] != image['id'] or actual['RootFS']['Layers'] != image['layers']:
                raise SnapshotError('container_snapshot_committed_image_changed')
        for source, receipt in manifest['mount_receipts'].items():
            verify_mount(directory / manifest['mounts'][source]['key'], receipt)
        return directory, manifest

    def verify(self, token):
        self.require_phase({'captured', 'quiesced'})
        self.assert_stopped()
        self.verified(token)
        return {'snapshot_verified': True}

    def restore(self, token):
        # A captured group is also fully stopped. Recreate its writable layers
        # when cloning the VM filesystem; old overlay mounts may carry stale
        # filesystem handles across kernels even though the data image is intact.
        self.require_phase({'quiesced', 'captured', 'resumed'})
        discard = self.state['phase'] == 'resumed'
        if discard:
            current = {name: self.engine.inspect(name) for name in self.names}
            if {name: info['Id'] for name, info in current.items()} != self.state['current_ids']:
                raise SnapshotError('container_snapshot_service_replaced_outside_checkpoint')
        else:
            current = self.assert_stopped()
        # Verify the original checkpoint before discarding an episode. Saving a
        # NEW checkpoint still requires quiesce(); reset never saves dirty state.
        directory, manifest = self.verified(token)
        if self.topology(current) != manifest['mounts']:
            raise SnapshotError('container_snapshot_mount_topology_changed')
        # Check immutable mounts and the entire snapshot set before deletion.
        for source, mount in manifest['mounts'].items():
            if not mount['writable']:
                observed = directory / mount['key'] / 'readonly-check.tar'
                if archive(source, observed, self.timeout) != manifest['mount_receipts'][source]['sha256']:
                    raise SnapshotError('container_snapshot_readonly_mount_changed')
                observed.unlink()
        bodies = {name: create_body(manifest['containers'][name], manifest['images'][name]['id'])
                  for name in self.names}
        self.save('restoring', replacements={}, pending=None)
        for name in reversed(self.names):
            self.save(pending={'operation': 'remove_stopped_container', 'container': name})
            self.engine.call('DELETE', '/containers/' + current[name]['Id'], query={'v': 'false', 'force': 'true' if discard else 'false'})
            self.save(pending=None)
        for source, mount in manifest['mounts'].items():
            if mount['writable']:
                self.save(pending={'operation': 'restore_mount', 'key': mount['key']})
                restore_mount(source, directory / mount['key'], manifest['mount_receipts'][source], self.timeout)
                self.save(pending=None)
        for name in self.names:
            self.save(pending={'operation': 'create_restored_container', 'container': name})
            result = self.engine.call('POST', '/containers/create', query={'name': name}, body=bodies[name])
            actual = self.engine.stopped(name)
            expected = manifest['containers'][name]
            if (actual['Id'] != result['Id'] or actual['Config'] != {**expected['Config'], 'Image': bodies[name]['Image']}
                    or mount_signature(actual) != mount_signature(expected)):
                raise SnapshotError('container_snapshot_restored_configuration_mismatch')
            # Verify every requested host field. Docker may add response-only
            # defaults, but must not change any persisted requested setting.
            changed = changed_host_fields(actual['HostConfig'], bodies[name]['HostConfig'])
            if changed:
                raise SnapshotError('container_snapshot_restored_host_configuration_mismatch:' + ','.join(changed))
            self.save(replacements={**self.state['replacements'], name: actual['Id']}, pending=None)
        self.save('restored', current_ids=self.state['replacements'], resume_containers=manifest['containers'])
        return {'predecessors': {name: info['Id'] for name, info in current.items()},
                'successors': self.state['current_ids'], 'storage_restored': True}

    def probe(self, name, info):
        probe = self.spec['readiness'][name]
        if probe['kind'] == 'exec':
            result = subprocess.run(['docker', 'exec', info['Id'], *probe['argv']],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return result.returncode == 0
        host = probe['host']
        if host == 'container':
            addresses = [net['IPAddress'] for net in info['NetworkSettings']['Networks'].values() if net.get('IPAddress')]
            if len(addresses) != 1:
                raise SnapshotError('container_snapshot_probe_address_ambiguous')
            host = addresses[0]
        if probe['kind'] == 'tcp':
            with socket.create_connection((host, probe['port']), timeout=2):
                return True
        client = HTTPConnection(host, probe['port'], timeout=2)
        try:
            client.request('GET', probe['path'], headers=probe.get('headers', {}))
            return client.getresponse().status in probe['statuses']
        finally:
            client.close()

    def start(self):
        """Start a new, explicitly owned stopped group in dependency order."""
        self.require_phase({'new'})
        current = {name: self.engine.stopped(name) for name in self.names}
        self.topology(current)
        self.save('starting', pending=None)
        running = self.start_services(current)
        self.check_deployments(running)
        self.save('resumed', current_ids={name: value['Id'] for name, value in running.items()})
        return {'running_containers': self.state['current_ids'], 'readiness_verified': True}

    def start_services(self, current):
        """Start independent applications together; release dependents when ready."""
        running, started = {}, {}
        dependencies = self.spec.get('dependencies')
        if dependencies is None:
            dependencies = {name: self.names[:i] for i, name in enumerate(self.names)}
        def ready(name):
            info = self.engine.inspect(name)
            if not info['State']['Running'] or info['State'].get('Restarting'):
                raise SnapshotError('container_snapshot_resumed_service_exited')
            healthy = info['State'].get('Health', {}).get('Status', 'healthy') == 'healthy'
            try:
                return info if healthy and self.probe(name, info) else None
            except (OSError, TimeoutError, HTTPException, subprocess.TimeoutExpired):
                return None

        with ThreadPoolExecutor(max_workers=len(self.names)) as pool:
            while len(running) < len(self.names):
                for name in self.names:
                    if name in started or not set(dependencies[name]) <= running.keys():
                        continue
                    self.save(pending={'operation': 'start', 'container': name})
                    self.engine.call('POST', '/containers/' + current[name]['Id'] + '/start')
                    started[name] = time.monotonic()
                waiting = [name for name in self.names if name in started and name not in running]
                for name, info in zip(waiting, pool.map(ready, waiting)):
                    if info is not None:
                        running[name] = info
                    elif time.monotonic() - started[name] >= self.spec['readiness_timeout_seconds']:
                        self.save(pending={'operation': 'readiness', 'container': name})
                        raise SnapshotError('container_snapshot_service_readiness_timeout')
                if len(running) < len(self.names):
                    time.sleep(0.2)
        self.save(pending=None)
        return running

    def resume(self):
        self.require_phase({'captured', 'restored'})
        current = self.assert_stopped()
        expected = self.state['resume_containers']
        self.save('resuming', pending=None)
        running = self.start_services(current)
        for name, info in running.items():
            actual_networks = info['NetworkSettings']['Networks']
            if set(actual_networks) != set(expected[name]['NetworkSettings']['Networks']):
                raise SnapshotError('container_snapshot_service_network_changed')
            for network, before in expected[name]['NetworkSettings']['Networks'].items():
                after = actual_networks[network]
                # Docker allocates the default bridge address when recreating a
                # container; only container-relative/published-port probes can
                # tolerate that. User-defined/static endpoints remain exact.
                dynamic_bridge = (network == 'bridge'
                                  and not before.get('IPAMConfig')
                                  and not after.get('IPAMConfig')
                                  and self.spec['readiness'][name].get('host', 'container') in
                                  {'container', '127.0.0.1', 'localhost', '::1'})
                keys = ['Gateway', 'IPv6Gateway', 'IPPrefixLen', 'GlobalIPv6PrefixLen']
                if not dynamic_bridge:
                    keys += ['IPAddress', 'GlobalIPv6Address']
                # The daemon recreates its built-in bridge in a new VM. Its
                # allocated NetworkID is not a stable application identity.
                if network != 'bridge':
                    keys.append('NetworkID')
                if any(before.get(key) != after.get(key) for key in keys):
                    raise SnapshotError('container_snapshot_service_address_changed')
            if info['NetworkSettings'].get('Ports') != expected[name]['NetworkSettings'].get('Ports'):
                raise SnapshotError('container_snapshot_service_published_ports_changed')
        self.save('resumed', current_ids={name: value['Id'] for name, value in current.items()})
        return {'running_containers': self.state['current_ids'], 'readiness_verified': True,
                'native_business_readback_required': True}

    def diagnose(self):
        """Preserve private service evidence before a caller closes a failed VM."""
        directory = self.store / 'diagnostics'
        directory.mkdir(mode=0o700, exist_ok=True)
        result = {'phase': self.state['phase'], 'start_order': self.names,
                  'stop_order': list(reversed(self.names)), 'pending': self.state.get('pending'),
                  'services': {}}
        for name in self.names:
            try:
                info = self.engine.inspect(name)
                state = info['State']
                result['services'][name] = {
                    'id': info['Id'], 'image': info['Image'],
                    'state': {key: state.get(key) for key in
                              ('Running', 'Restarting', 'OOMKilled', 'ExitCode', 'StartedAt', 'FinishedAt')},
                    'health': state.get('Health', {}).get('Status'),
                    'networks': info['NetworkSettings']['Networks'],
                    'ports': info['NetworkSettings']['Ports'],
                }
                log = subprocess.run(['docker', 'logs', '--timestamps', '--tail', '80', info['Id']],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                target = directory / (name + '.log')
                target.write_bytes(log.stdout[-65536:])
                target.chmod(0o600)
            except Exception as error:
                result['services'][name] = {'diagnostic_error': type(error).__name__}
        save_private(directory / 'summary.json', result)
        return {'directory': str(directory), 'services': list(result['services'])}
