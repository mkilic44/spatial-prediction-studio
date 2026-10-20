"""docs/METHODS.md is generated from the method registry and must not drift from it."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_methods_reference_matches_registry():
    spec = importlib.util.spec_from_file_location('make_methods_doc', ROOT / 'tools/make_methods_doc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(['--check']) == 0, 'Run python tools/make_methods_doc.py and commit docs/METHODS.md'
