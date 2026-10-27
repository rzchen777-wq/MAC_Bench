"""Load the current baseline configuration templates."""
from pathlib import Path
import yaml
from .paths import repository_root


def load_method_config(name: str) -> dict:
    if name not in {"react_single", "aorchestra", "magentic_one", "meta_team"}:
        raise ValueError("Unknown baseline: " + name)
    path = repository_root()/"configs/baselines"/(name+".yaml")
    result = yaml.safe_load(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("Baseline configuration must be a mapping")
    return result
