"""Typed Work IR: entities, versioned resources, and work-unit contracts.

This is the only structure the Stage I compiler accepts. Edges are not part of
the IR; they are compiled from producer/consumer resource flow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping, Sequence

ResourceKind = Literal["artifact", "state", "information", "decision", "constraint"]
ResourceOrigin = Literal["task_spec", "initial_environment", "node"]
ResourceCategory = Literal["value", "state"]
ConsumeMode = Literal["read", "constrain", "update"]
ProduceOperation = Literal["create", "update"]
AccessMode = Literal["R", "W"]

RESOURCE_KINDS: frozenset[str] = frozenset(
    {"artifact", "state", "information", "decision", "constraint"}
)
RESOURCE_ORIGINS: frozenset[str] = frozenset(
    {"task_spec", "initial_environment", "node"}
)
RESOURCE_CATEGORIES: frozenset[str] = frozenset({"value", "state"})
CONSUME_MODES: frozenset[str] = frozenset({"read", "constrain", "update"})
PRODUCE_OPERATIONS: frozenset[str] = frozenset({"create", "update"})
ACCESS_MODES: frozenset[str] = frozenset({"R", "W"})

ENTITY_ID_RE = re.compile(r"^ent\.[a-z][a-z0-9_]*$")
RESOURCE_ID_RE = re.compile(r"^res\.[a-z][a-z0-9_]*@\d+$")
WORK_ID_RE = re.compile(r"^work\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
ACCEPTANCE_ID_RE = re.compile(r"^acc\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")

TYPE_ALIASES: dict[str, str] = {
    "db.schema": "database.schema",
    "sql.schema": "database.schema",
    "sql.ddl": "database.schema",
    "api": "software.api",
    "source": "software.source_tree",
    "src": "software.source_tree",
}


class IRError(ValueError):
    """The IR document is structurally invalid."""


def normalize_domain_type(raw: str) -> str:
    text = str(raw or "").strip().lower().replace(" ", "_")
    if not text:
        raise IRError("resource domain type must be non-empty")
    return TYPE_ALIASES.get(text, text)


def slugify(text: str, *, fallback: str = "item") -> str:
    chars = [ch.lower() if ch.isalnum() else "_" for ch in str(text or "")]
    slug = re.sub(r"_+", "_", "".join(chars)).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"{fallback}_{slug}" if slug else fallback
    return slug[:48]


def parse_resource_id(resource_id: str) -> tuple[str, int]:
    if not RESOURCE_ID_RE.match(resource_id):
        raise IRError(f"invalid resource id {resource_id!r}")
    base, _, version = resource_id.rpartition("@")
    return base, int(version)


def make_resource_id(base: str, version: int) -> str:
    if not base.startswith("res."):
        base = f"res.{slugify(base, fallback='resource')}"
    if version < 0:
        raise IRError("resource version must be >= 0")
    resource_id = f"{base}@{version}"
    if not RESOURCE_ID_RE.match(resource_id):
        raise IRError(f"invalid resource id {resource_id!r}")
    return resource_id


def identity_key(kind: str, domain_type: str, about: Sequence[str]) -> tuple[str, str, tuple[str, ...]]:
    return (kind, normalize_domain_type(domain_type), tuple(sorted(set(about))))


@dataclass(frozen=True)
class Entity:
    id: str
    type: str
    name: str
    definition: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {"id": self.id, "type": self.type, "name": self.name}
        if self.definition:
            payload["definition"] = self.definition
        return payload

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Entity":
        entity_id = str(raw.get("id") or "").strip()
        entity_type = str(raw.get("type") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not ENTITY_ID_RE.match(entity_id):
            raise IRError(f"invalid entity id {entity_id!r}")
        if not entity_type or not name:
            raise IRError(f"entity {entity_id} is missing type or name")
        return cls(
            id=entity_id,
            type=entity_type,
            name=name,
            definition=str(raw.get("definition") or "").strip(),
        )


@dataclass(frozen=True)
class Provenance:
    origin: ResourceOrigin
    producer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"origin": self.origin, "producer": self.producer}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "Provenance":
        payload = dict(raw or {})
        origin = str(payload.get("origin") or "").strip()
        if origin not in RESOURCE_ORIGINS:
            raise IRError(f"invalid resource origin {origin!r}")
        producer = payload.get("producer")
        producer_id = None if producer in (None, "", "null") else str(producer)
        if origin == "node":
            if not producer_id or not WORK_ID_RE.match(producer_id):
                raise IRError("node-origin resource must name a work-unit producer")
        elif producer_id is not None:
            raise IRError(f"{origin} resource must not have a producer")
        return cls(origin=origin, producer=producer_id)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Resource:
    id: str
    kind: ResourceKind
    type: str
    about: tuple[str, ...]
    definition: str
    provenance: Provenance
    version: int
    base_resource: str
    category: ResourceCategory = "value"

    def identity(self) -> tuple[str, str, tuple[str, ...]]:
        return identity_key(self.kind, self.type, self.about)

    def with_producer(self, work_id: str) -> "Resource":
        return replace(
            self,
            provenance=Provenance(origin="node", producer=work_id),
        )

    @property
    def is_state(self) -> bool:
        return self.category == "state"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "type": self.type,
            "about": list(self.about),
            "definition": self.definition,
            "provenance": self.provenance.to_dict(),
            "version": self.version,
            "base_resource": self.base_resource,
            "category": self.category,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Resource":
        resource_id = str(raw.get("id") or "").strip()
        base, version = parse_resource_id(resource_id)
        kind = str(raw.get("kind") or "").strip()
        if kind not in RESOURCE_KINDS:
            raise IRError(f"resource {resource_id} has invalid kind {kind!r}")
        domain_type = normalize_domain_type(str(raw.get("type") or ""))
        about_raw = raw.get("about") or []
        if not isinstance(about_raw, Sequence) or isinstance(about_raw, (str, bytes)):
            raise IRError(f"resource {resource_id} about must be a list")
        about = tuple(str(item) for item in about_raw)
        for entity_id in about:
            if not ENTITY_ID_RE.match(entity_id):
                raise IRError(f"resource {resource_id} about contains invalid entity {entity_id!r}")
        declared_version = raw.get("version", version)
        try:
            parsed_version = int(declared_version)
        except (TypeError, ValueError) as exc:
            raise IRError(f"resource {resource_id} has invalid version") from exc
        if parsed_version != version:
            raise IRError(f"resource {resource_id} version does not match id")
        base_resource = str(raw.get("base_resource") or base).strip()
        if base_resource != base:
            raise IRError(f"resource {resource_id} base_resource does not match id")
        definition = str(raw.get("definition") or "").strip()
        if not definition:
            raise IRError(f"resource {resource_id} is missing definition")
        category = str(raw.get("category") or "value").strip()
        if category not in RESOURCE_CATEGORIES:
            raise IRError(f"resource {resource_id} has invalid category {category!r}")
        return cls(
            id=resource_id,
            kind=kind,  # type: ignore[arg-type]
            type=domain_type,
            about=about,
            definition=definition,
            provenance=Provenance.from_mapping(raw.get("provenance")),
            version=parsed_version,
            base_resource=base_resource,
            category=category,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ResourceUse:
    resource_id: str
    mode: ConsumeMode = "read"

    def to_dict(self) -> dict[str, Any]:
        return {"resource": self.resource_id, "mode": self.mode}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResourceUse":
        resource_id = str(raw.get("resource") or raw.get("resource_id") or "").strip()
        parse_resource_id(resource_id)
        mode = str(raw.get("mode") or "read").strip()
        if mode not in CONSUME_MODES:
            raise IRError(f"invalid consume mode {mode!r}")
        return cls(resource_id=resource_id, mode=mode)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ResourceProduce:
    resource_id: str
    operation: ProduceOperation = "create"

    def to_dict(self) -> dict[str, Any]:
        return {"resource": self.resource_id, "operation": self.operation}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResourceProduce":
        resource_id = str(raw.get("resource") or raw.get("resource_id") or "").strip()
        parse_resource_id(resource_id)
        operation = str(raw.get("operation") or "create").strip()
        if operation not in PRODUCE_OPERATIONS:
            raise IRError(f"invalid produce operation {operation!r}")
        return cls(resource_id=resource_id, operation=operation)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Access:
    """A work unit's access to a persistent mutable state resource.

    ``state_id`` references a state-category resource in the registry.
    ``scope`` is a hierarchical dot-path describing a **state region**
    (e.g. "contacts.foreign_keys", "Contact.company", or "" for the whole state).
    ``mode`` is "R" (read) or "W" (write).
    ``effect`` (required for W) describes what the write does.
    ``assumption`` (required for R) describes what the read depends on.

    W without effect → downgraded to R during materialization.
    R without assumption → no W/R coupling (can't prove interference).
    """

    state_id: str
    scope: str
    mode: AccessMode
    effect: str = ""
    assumption: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state_id,
            "scope": self.scope,
            "mode": self.mode,
        }
        if self.effect:
            payload["effect"] = self.effect
        if self.assumption:
            payload["assumption"] = self.assumption
        return payload

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Access":
        state_id = str(raw.get("state") or raw.get("state_id") or "").strip()
        parse_resource_id(state_id)
        scope = str(raw.get("scope") or "").strip()
        mode = str(raw.get("mode") or "R").strip().upper()
        if mode not in ACCESS_MODES:
            raise IRError(f"invalid access mode {mode!r} (must be R or W)")
        effect = str(raw.get("effect") or "").strip()
        assumption = str(raw.get("assumption") or raw.get("purpose") or "").strip()
        return cls(
            state_id=state_id,
            scope=scope,
            mode=mode,  # type: ignore[arg-type]
            effect=effect,
            assumption=assumption,
        )


def scope_overlap(a: str, b: str) -> bool:
    """Return True if two hierarchical scopes overlap.

    Two scopes overlap if one is a prefix of the other (parent-child relation)
    or they are exactly equal. Siblings (e.g. "Contact" and "Opportunity") do
    NOT overlap. An empty scope means the whole state and overlaps everything.
    """
    a = a.strip(".")
    b = b.strip(".")
    if not a or not b:
        return True
    if a == b:
        return True
    if a.startswith(b + "."):
        return True
    if b.startswith(a + "."):
        return True
    return False


@dataclass(frozen=True)
class Acceptance:
    id: str
    targets: tuple[str, ...]
    statement: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "targets": list(self.targets),
            "statement": self.statement,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Acceptance":
        acc_id = str(raw.get("id") or "").strip()
        if not ACCEPTANCE_ID_RE.match(acc_id):
            raise IRError(f"invalid acceptance id {acc_id!r}")
        statement = str(raw.get("statement") or "").strip()
        if not statement:
            raise IRError(f"acceptance {acc_id} is missing statement")
        targets_raw = raw.get("targets") or []
        if not isinstance(targets_raw, Sequence) or isinstance(targets_raw, (str, bytes)):
            raise IRError(f"acceptance {acc_id} targets must be a list")
        targets = tuple(str(item) for item in targets_raw)
        for resource_id in targets:
            parse_resource_id(resource_id)
        return cls(id=acc_id, targets=targets, statement=statement)


@dataclass(frozen=True)
class WorkUnit:
    id: str
    goal: str
    consumes: tuple[ResourceUse, ...]
    produces: tuple[ResourceProduce, ...]
    acceptance: tuple[Acceptance, ...]
    accesses: tuple[Access, ...] = ()

    @property
    def input_ids(self) -> frozenset[str]:
        return frozenset(item.resource_id for item in self.consumes)

    @property
    def output_ids(self) -> frozenset[str]:
        return frozenset(item.resource_id for item in self.produces)

    @property
    def state_access_ids(self) -> frozenset[str]:
        return frozenset(item.state_id for item in self.accesses)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "goal": self.goal,
            "consumes": [item.to_dict() for item in self.consumes],
            "produces": [item.to_dict() for item in self.produces],
            "acceptance": [item.to_dict() for item in self.acceptance],
        }
        if self.accesses:
            payload["accesses"] = [item.to_dict() for item in self.accesses]
        return payload

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkUnit":
        work_id = str(raw.get("id") or "").strip()
        if not WORK_ID_RE.match(work_id):
            raise IRError(f"invalid work unit id {work_id!r}")
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            raise IRError(f"work unit {work_id} is missing goal")
        consumes = _mapping_list(raw.get("consumes"), ResourceUse.from_mapping, f"{work_id}.consumes")
        produces = _mapping_list(raw.get("produces"), ResourceProduce.from_mapping, f"{work_id}.produces")
        acceptance = _mapping_list(raw.get("acceptance"), Acceptance.from_mapping, f"{work_id}.acceptance")
        accesses = _mapping_list(raw.get("accesses"), Access.from_mapping, f"{work_id}.accesses")
        if not produces:
            raise IRError(f"work unit {work_id} must produce at least one resource")
        if not acceptance:
            raise IRError(f"work unit {work_id} must declare at least one acceptance obligation")
        produced = {item.resource_id for item in produces}
        consumed = {item.resource_id for item in consumes}
        for obligation in acceptance:
            unknown = [rid for rid in obligation.targets if rid not in produced and rid not in consumed]
            if unknown:
                raise IRError(
                    f"acceptance {obligation.id} targets unknown resources {unknown}"
                )
        return cls(
            id=work_id,
            goal=goal,
            consumes=consumes,
            produces=produces,
            acceptance=acceptance,
            accesses=accesses,
        )


@dataclass(frozen=True)
class TaskIR:
    """Full task IR. ``work_units`` are the current contract nodes (often leaves)."""

    entities: tuple[Entity, ...]
    resources: tuple[Resource, ...]
    work_units: tuple[WorkUnit, ...]
    root_id: str
    task_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def entity_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.entities)

    def resource_map(self) -> dict[str, Resource]:
        return {item.id: item for item in self.resources}

    def work_map(self) -> dict[str, WorkUnit]:
        return {item.id: item for item in self.work_units}

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "root_id": self.root_id,
            "entities": [item.to_dict() for item in self.entities],
            "resources": [item.to_dict() for item in self.resources],
            "work_units": [item.to_dict() for item in self.work_units],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TaskIR":
        entities = _mapping_list(raw.get("entities"), Entity.from_mapping, "entities")
        resources = _mapping_list(raw.get("resources"), Resource.from_mapping, "resources")
        work_units = _mapping_list(raw.get("work_units"), WorkUnit.from_mapping, "work_units")
        if not work_units:
            raise IRError("TaskIR must contain at least one work unit")
        entity_ids = {item.id for item in entities}
        if len(entity_ids) != len(entities):
            raise IRError("duplicate entity ids")
        resource_ids = {item.id for item in resources}
        if len(resource_ids) != len(resources):
            raise IRError("duplicate resource ids")
        work_ids = {item.id for item in work_units}
        if len(work_ids) != len(work_units):
            raise IRError("duplicate work unit ids")
        for resource in resources:
            unknown = [entity_id for entity_id in resource.about if entity_id not in entity_ids]
            if unknown:
                raise IRError(f"resource {resource.id} about unknown entities {unknown}")
        for unit in work_units:
            missing_in = sorted(unit.input_ids - resource_ids)
            missing_out = sorted(unit.output_ids - resource_ids)
            if missing_in or missing_out:
                raise IRError(
                    f"work unit {unit.id} references unknown resources "
                    f"in={missing_in} out={missing_out}"
                )
            missing_acc = sorted(unit.state_access_ids - resource_ids)
            if missing_acc:
                raise IRError(
                    f"work unit {unit.id} accesses unknown state resources {missing_acc}"
                )
        root_id = str(raw.get("root_id") or work_units[0].id).strip()
        if root_id not in work_ids:
            raise IRError(f"root_id {root_id!r} is not a work unit")
        return cls(
            entities=entities,
            resources=resources,
            work_units=work_units,
            root_id=root_id,
            task_id=str(raw.get("task_id") or "").strip(),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True)
class TaskInput:
    """Stage I ingestion: specification plus optional environment inventory."""

    task_id: str
    specification: str
    environment_inventory: str = ""
    environment_resources: tuple[Resource, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "specification": self.specification,
            "environment_inventory": self.environment_inventory,
            "environment_resources": [item.to_dict() for item in self.environment_resources],
            "metadata": dict(self.metadata),
        }


def _mapping_list(raw: Any, factory, label: str) -> tuple[Any, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise IRError(f"{label} must be a list")
    return tuple(factory(item) for item in raw)
