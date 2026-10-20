"""docs/METHODS.md is generated from the method registry and must not drift from it."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_methods_reference_matches_registry():
    spec = importlib.util.spec_from_file_location('make_methods_doc', ROOT / 'tools/make_methods_doc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(['--check']) == 0, 'Run python tools/make_methods_doc.py and commit docs/METHODS.md'


def test_every_cited_source_has_a_checked_record():
    spec = importlib.util.spec_from_file_location('make_methods_doc', ROOT / 'tools/make_methods_doc.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    spec = importlib.util.spec_from_file_location('references', ROOT / 'tools/references.py')
    references = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(references)
    entries = generator.load_references()
    for key in generator.cited_keys():
        assert entries[key].get('metadata'), key
        # The stored record still matches what the source must be.
        assert references.problems(key, entries[key], entries[key]['metadata']) == [], key
    text = (ROOT / 'docs/METHODS.md').read_text(encoding='utf-8')
    assert all('#ref-' + key in text for key in generator.cited_keys())


def test_author_supplements_are_backed_by_their_stated_source():
    import importlib
    import json
    entries = json.loads((ROOT / 'docs/references.json').read_text(encoding='utf-8'))['references']
    for key, entry in entries.items():
        check = entry.get('supplement', {}).get('check')
        if not check:
            continue
        module, _, name = check['python'].rpartition('.')
        text = getattr(importlib.import_module(module), name).__doc__
        assert all(item in text for item in check['contains']), key
