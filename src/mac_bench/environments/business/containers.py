"""Docker Engine operations for restoring portable business environments."""
import json
import socket
from copy import deepcopy
from http.client import HTTPConnection
from urllib.parse import quote, urlencode

class BusinessEnvironmentError(Exception):
    pass


class UnixConnection(HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


class Engine:
    def __init__(self, timeout):
        self.timeout = timeout
        self.prefix = ''
        version = self.call('GET', '/version')
        selected = min(tuple(map(int, version['ApiVersion'].split('.'))), (1, 45))
        minimum = tuple(map(int, version.get('MinAPIVersion', '1.24').split('.')))
        if selected < max(minimum, (1, 41)):
            raise BusinessEnvironmentError('business_environment_engine_version_unsupported')
        # Use a stable contract even when a newer daemon is installed.
        self.prefix = '/v' + '.'.join(map(str, selected))

    def call(self, method, path, *, query=None, body=None, statuses=(200, 201, 204)):
        client = UnixConnection('localhost', timeout=self.timeout)
        try:
            url = self.prefix + path + ('?' + urlencode(query) if query else '')
            payload = None if body is None else json.dumps(body, allow_nan=False).encode()
            client.request(method, url, body=payload, headers={'Content-Type': 'application/json'})
            response = client.getresponse()
            data = response.read()
            if response.status not in statuses:
                raise BusinessEnvironmentError('business_environment_engine_request_failed:' + str(response.status))
            return json.loads(data) if data else None
        finally:
            client.close()

    def inspect(self, name):
        return self.call('GET', '/containers/' + quote(name, safe='') + '/json')


def create_body(info, image):
    """Preserve native configuration and reuse every existing volume identity.

    An image commit excludes mounts. In particular, anonymous image volumes must
    be rebound by their actual names, not silently replaced by empty new volumes.
    """
    body = deepcopy(info['Config'])
    body['Image'] = image
    host = deepcopy(info['HostConfig'])
    configured = {mount['Target'] for mount in host.get('Mounts') or []}
    for bind in host.get('Binds') or []:
        parts = bind.split(':')
        if len(parts) not in (2, 3):
            raise BusinessEnvironmentError('business_environment_bind_syntax_unsupported')
        configured.add(parts[1])
    extra = []
    for mount in info['Mounts']:
        if mount['Destination'] in configured:
            continue
        if mount['Type'] != 'volume':
            raise BusinessEnvironmentError('business_environment_implicit_mount_unsupported')
        extra.append({'Type': 'volume', 'Source': mount['Name'], 'Target': mount['Destination'],
                      'ReadOnly': not mount['RW'], 'VolumeOptions': {'NoCopy': True}})
    if extra:
        host['Mounts'] = [*(host.get('Mounts') or []), *extra]
    # Pin dynamically allocated published ports too. Reallocation would break
    # the existing authenticated transports, even if the container is healthy.
    ports = info['NetworkSettings'].get('Ports') or {}
    for port, bindings in ports.items():
        if bindings:
            host.setdefault('PortBindings', {})[port] = deepcopy(bindings)
    host['PublishAllPorts'] = False
    body['HostConfig'] = host
    endpoints = {}
    for name, network in info['NetworkSettings']['Networks'].items():
        endpoint = {key: deepcopy(network[key]) for key in ('IPAMConfig', 'Links', 'Aliases', 'DriverOpts')
                    if network.get(key) is not None}
        # Docker's default bridge does not permit a requested static address.
        if name != 'bridge':
            ipam = dict(endpoint.get('IPAMConfig') or {})
            for actual, requested in (('IPAddress', 'IPv4Address'), ('GlobalIPv6Address', 'IPv6Address')):
                if network.get(actual):
                    ipam[requested] = network[actual]
            if ipam:
                endpoint['IPAMConfig'] = ipam
        # Auto-generated container-ID aliases refer to the predecessor only.
        if 'Aliases' in endpoint:
            endpoint['Aliases'] = [alias for alias in endpoint['Aliases']
                                   if alias not in {info['Id'], info['Id'][:12]}]
        endpoints[name] = endpoint
    body['NetworkingConfig'] = {'EndpointsConfig': endpoints}
    return body
