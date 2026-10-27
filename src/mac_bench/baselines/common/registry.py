"""Task-API registry, separate from the older stream-method registry."""
import importlib

METHODS = {
    'react_single': ('mac_bench.baselines.react_single.tasks','ReactSingle'),
    'aorchestra': ('mac_bench.baselines.aorchestra.tasks','AOrchestra'),
    'meta_team': ('mac_bench.baselines.meta_team.tasks','MetaTeam'),
    'magentic_one': ('mac_bench.baselines.magentic_one.tasks','MagenticOne'),
}


def create_method(name, *, config=None):
    try:module, symbol=METHODS[name]
    except KeyError:raise ValueError('Unknown collaboration method: '+name) from None
    implementation=getattr(importlib.import_module(module),symbol)
    return implementation(config)
