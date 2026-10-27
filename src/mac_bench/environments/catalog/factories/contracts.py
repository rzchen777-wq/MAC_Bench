"""Explicit app adapters and native object handles; no symbolic world IDs."""
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from mac_bench.environments.catalog.entities import CatalogError


@dataclass(frozen=True)
class NativeObject:
    """An app identity, its exact catalog binding, and independently read data."""
    entity: str
    binding: str
    identity: Mapping[str, Any]
    data: Mapping[str, Any]


@dataclass(frozen=True)
class NativeProvider:
    """Bind existing app infrastructure, including nested/lifecycle creation.

    prepare(parameters, parents) is pure and returns the native creation payload.
    validate(payload, parents) performs app-specific preflight, raising on failure.
    create(payload, parents) performs the native operation and returns its receipt.
    identify(record) extracts the complete native locator from receipt/readback.
    read(identity, parents) performs an independent native read.
    verify(payload, record, parents) checks business invariants after creation.
    read_parent(handle) independently reads each parent, returning a NativeObject.

    All callbacks are explicit: the infrastructure never fabricates HTTP routes,
    field projections, lifecycle actions or successful readback. Adapters can wrap
    the existing Frappe/Pretix/Twenty/BigCapital app factories and native clients.
    """
    prepare: Callable
    validate: Callable
    create: Callable
    identify: Callable
    read: Callable
    verify: Callable
    read_parent: Callable | None = None


@dataclass(frozen=True)
class CreationResult:
    factory: str
    factory_revision: str
    object: NativeObject
    receipt: Any


class FactoryExecutionError(CatalogError):
    """No automatic retry: a failed write/readback may have created an object."""
    def __init__(self, *, factory, phase, side_effects_possible, identity=None, receipt=None):
        super().__init__('factory_execution_failed', factory=factory, phase=phase,
                         side_effects_possible=side_effects_possible, identity=identity)
        self.phase = phase
        self.side_effects_possible = side_effects_possible
        self.identity = identity
        self.receipt = receipt


@dataclass(frozen=True)
class BatchResult:
    """Stops at first failure; successful objects remain real and are returned."""
    completed: tuple[CreationResult, ...]
    failed_index: int | None
    error: Exception | None
