"""Canonical JSON, versioned artifacts and lazy native-evidence access."""
import gzip
import json
import re
import tempfile
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from mac_bench.environments.catalog.entities.errors import CatalogError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return sha256(encoded(value)).hexdigest()


def read_json(path):
    path = Path(path)
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = encoded(value) + b'\n'
    if path.suffix == '.gz':
        raw = gzip.compress(raw, mtime=0)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def pointer(*parts):
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def at(document, path):
    if path == '':
        return document
    if not isinstance(path, str) or not path.startswith('/'):
        raise CatalogError('invalid_pointer', pointer=path)
    value = document
    try:
        for part in path[1:].split('/'):
            if re.search(r'~(?![01])', part):
                raise KeyError(part)
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not re.fullmatch('0|[1-9][0-9]*', part):
                    raise KeyError(part)
                value = value[int(part)]
            else:
                value = value[part]
    except (KeyError, IndexError, TypeError):
        raise CatalogError('evidence_pointer_missing', pointer=path) from None
    return value


class EvidenceStore:
    """Read one pinned source on demand; normal queries never load raw evidence."""
    def __init__(self, manifest_path):
        self.path = Path(manifest_path).resolve()
        self.manifest = read_json(self.path)
        if self.manifest.get('version') != 1 or not self.manifest.get('applications'):
            raise CatalogError('evidence_manifest_invalid')
        self._cache = {}

    def document(self, application):
        if application not in self.manifest['applications']:
            raise CatalogError('evidence_application_missing', application=application)
        if application not in self._cache:
            spec = self.manifest['applications'][application]
            path = (self.path.parent / spec['path']).resolve()
            if not path.is_relative_to(self.path.parent):
                raise CatalogError('evidence_path_outside_manifest')
            if sha256(path.read_bytes()).hexdigest() != spec['sha256']:
                raise CatalogError('evidence_digest_mismatch', application=application)
            self._cache[application] = read_json(path)
        return self._cache[application]

    def reference(self, application, path):
        at(self.document(application), path)
        return {'application': application, 'sha256': self.manifest['applications'][application]['sha256'], 'pointer': path}

    def resolve(self, reference):
        application = reference['application']
        spec = self.manifest['applications'].get(application)
        if spec is None or reference.get('sha256') != spec['sha256']:
            raise CatalogError('evidence_reference_version_mismatch')
        return deepcopy(at(self.document(application), reference['pointer']))
