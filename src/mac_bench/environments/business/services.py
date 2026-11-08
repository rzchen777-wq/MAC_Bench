"""Start task applications in dependency order and wait for native readiness."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPException
import socket
import subprocess
import time
from .containers import BusinessEnvironmentError

def service_order(spec):
    """Resolve the explicit dependencies of every service."""
    names = spec['containers']
    dependencies = spec.get('dependencies')
    if (not isinstance(dependencies, dict) or set(dependencies) != set(names)
            or any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                   or len(items) != len(set(items)) or any(item not in names or item == name for item in items)
                   for name, items in dependencies.items())):
        raise BusinessEnvironmentError('business_environment_dependencies_invalid')
    ordered, remaining = [], set(names)
    while remaining:
        ready = [name for name in names if name in remaining
                 and not remaining.intersection(dependencies[name])]
        if not ready:
            raise BusinessEnvironmentError('business_environment_dependency_cycle')
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


class BusinessServices:
    def __init__(self, engine, *, names, dependencies, readiness, timeout=300):
        self.engine = engine
        self.names = service_order({'containers': names, 'dependencies': dependencies})
        self.dependencies = dependencies
        self.readiness = readiness
        self.timeout = timeout

    def probe(self, name, info):
        probe = self.readiness[name]
        if probe['kind'] == 'exec':
            result = subprocess.run(['docker', 'exec', info['Id'], *probe['argv']],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return result.returncode == 0
        host = probe['host']
        if host == 'container':
            addresses = [net['IPAddress'] for net in info['NetworkSettings']['Networks'].values() if net.get('IPAddress')]
            if len(addresses) != 1:
                raise BusinessEnvironmentError('business_environment_probe_address_ambiguous')
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

    def start(self, current):
        """Start independent applications together; release dependents when ready."""
        running, started = {}, {}
        dependencies = self.dependencies
        def ready(name):
            info = self.engine.inspect(name)
            if not info['State']['Running'] or info['State'].get('Restarting'):
                raise BusinessEnvironmentError('business_environment_resumed_service_exited')
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
                    self.engine.call('POST', '/containers/' + current[name]['Id'] + '/start')
                    started[name] = time.monotonic()
                waiting = [name for name in self.names if name in started and name not in running]
                for name, info in zip(waiting, pool.map(ready, waiting)):
                    if info is not None:
                        running[name] = info
                    elif time.monotonic() - started[name] >= self.timeout:
                        raise BusinessEnvironmentError('business_environment_service_readiness_timeout')
                if len(running) < len(self.names):
                    time.sleep(0.2)
        return running
