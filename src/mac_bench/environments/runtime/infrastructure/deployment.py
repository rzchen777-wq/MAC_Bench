"""Validated application deployment metadata."""
from __future__ import annotations

import re
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
