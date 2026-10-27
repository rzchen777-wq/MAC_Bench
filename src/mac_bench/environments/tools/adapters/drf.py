"""Live compiler; packaging embeds this canonical implementation verbatim.

Frozen tool presentations must not depend on a stale compiler in an old native
snapshot. Keep the implementation in one place for ordinary development.
"""
from mac_bench.environments.catalog.factories.apps.drf import schema_for

__all__ = ['schema_for']
