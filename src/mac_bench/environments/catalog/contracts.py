"""Source provenance and executable entity/operation/factory contracts."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable

from jsonschema import Draft202012Validator

from mac_bench.environments.catalog.schema import _pointer
from mac_bench.environments.catalog.transitions import TransitionContract
from mac_bench.environments.errors import CompileFailure


@dataclass(frozen=True)
class Source:
    uri: str
    digest: str
    pointer: str

    @classmethod
    def capture(cls, uri: str, document: dict, pointer: str) -> Source:
        if not uri or not pointer.startswith('/'):
            raise CompileFailure('catalog_source_requires_uri_and_pointer')
        _pointer(document, pointer)
        encoded = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
        return cls(uri, sha256(encoded).hexdigest(), pointer)



@dataclass(frozen=True)
class Relation:
    source_entity: str
    source_field: str | None
    target_entity: str
    semantics: str
    provenance: str
    support: tuple[str, ...]
    evidence: tuple[dict, ...] = ()
    join_keys: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = ()
    cardinality: str | None = None

    @property
    def support_count(self):
        return len(set(self.support))



@dataclass(frozen=True)
class Operation:
    name: str
    entity: str
    role: str
    input_schema: dict
    writes: frozenset[str]
    source: Source
    execute: Callable
    interaction_cost: tuple[int, int] | None = None
    capabilities: frozenset[str] = frozenset()
    transition: TransitionContract | None = None

    def affects(self, entity, field):
        if self.role != 'update':
            return False
        return ((self.entity == entity and field in self.writes)
                or (self.transition is not None and any(
                    effect.entity == entity and field in effect.effects
                    for effect in self.transition.affected.values())))

    def invoke(self, arguments: dict):
        error = next(Draft202012Validator(self.input_schema).iter_errors(arguments), None)
        if error:
            raise CompileFailure(f'operation_arguments:{self.entity}:{self.name}:{list(error.path)}')
        return self.execute(deepcopy(arguments))



@dataclass(frozen=True)
class Factory:
    """Adapter methods use native identities; the materializer owns translation.

    validate receives the complete row and checks native-specific invariants
    without I/O. create receives declared input fields, including aggregates,
    and already resolved parents. Observed derived relations are not inputs.
    Deferred relations are attached only after both identities exist. read
    returns the entire projected record, never a merge with expected values.
    """
    entity: str
    create_operation: str | None
    read_operation: str
    validate: Callable[[dict], None]
    deferred_fields: frozenset[str] = frozenset()
    link_operation: str | None = None
    creation_fields: frozenset[str] | None = None
    prepare_creation: Callable[[dict], dict] | None = None

    def creation_values(self, row, fields):
        """Pure adapter projection, shared by dependency planning and writes.

        The adapter may omit native read-only children but must preserve the
        declared root input fields. It receives symbolic identities; native
        identity conversion happens afterwards in the materializer.
        """
        names = set(fields) if self.creation_fields is None else self.creation_fields
        values = (self.prepare_creation(deepcopy(row)) if self.prepare_creation is not None
                  else {name: deepcopy(row[name]) for name in names})
        if not isinstance(values, dict) or set(values) != names:
            raise CompileFailure('factory_prepared_creation_fields_mismatch')
        return values
