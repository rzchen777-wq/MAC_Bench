"""Synthetic task fixtures use a fixed minimal tool contract."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import pytest

@pytest.fixture(autouse=True)
def synthetic_tool_interface(monkeypatch):
    from mac_bench.environments.packaging import interface
    original = interface.presentation
    root = Path(__file__).resolve().parents[1] / 'fixtures'
    raw = (root/'tool_interface/manifest.json').read_bytes()
    fixture = SimpleNamespace(root=root, manifest={'metadata':{'tool_presentation':{
        'path':'tool_interface','version':2,'sha256':sha256(raw).hexdigest()}}})
    def presentation(bundle=None):
        selected = bundle.manifest.get('metadata',{}).get('tool_presentation') if bundle else None
        return original(bundle if selected else fixture)
    monkeypatch.setattr(interface,'presentation',presentation)
