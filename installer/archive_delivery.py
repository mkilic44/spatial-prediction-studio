"""Archive installer sources and bounded evidence after actual setup acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acceptance', type=Path, required=True)
    parser.add_argument('--build-record', type=Path, default=ROOT / 'installer/dist/installer-build-record.json')
    parser.add_argument('--evidence-dir', type=Path, default=ROOT / 'installer/final-acceptance')
    args = parser.parse_args()
    acceptance = args.acceptance.resolve()
    # sps-ç and sps-ç2 locate the Turkish 0.5.0 acceptance records; keep as recorded.
    if not (acceptance.is_relative_to(ROOT / 'installer') or acceptance in (
            ROOT.parent / 'sps-ç/setup-acceptance.json', ROOT.parent / 'sps-ç2/setup-acceptance.json',
            ROOT.parent / 'sps-cs/setup-acceptance.json')):
        raise ValueError('Expected installer-owned acceptance evidence')
    result = json.loads(acceptance.read_text(encoding='utf-8'))
    if result['status'] != 'passed':
        raise ValueError('A failed or pending installation cannot be archived as accepted')
    build_path = args.build_record.resolve()
    evidence_directory = args.evidence_dir.resolve()
    if not build_path.is_relative_to(ROOT / 'installer') or not evidence_directory.is_relative_to(ROOT / 'installer'):
        raise ValueError('Build and mirrored evidence must stay inside installer directory')
    build = json.loads(build_path.read_text())
    if sha(Path(build['installer'])) != build['sha256'] or result['installer_sha256'] != build['sha256']:
        raise ValueError('Setup changed after isolated acceptance')
    sources = {p for p in (ROOT / 'installer').iterdir()
               if p.is_file() and p.suffix in {'.py', '.iss', '.R', '.txt', '.md'}}
    # KURULUM_REHBERI.md is the installation guide file name recorded in the Turkish 0.5.0 payload; keep as recorded.
    sources.update({ROOT / 'tools/build_installer.py', ROOT / 'docs/KURULUM_REHBERI.md'})
    sources.update(p for p in (ROOT / 'mcp-server').rglob('*') if p.is_file()
                   and p.suffix.lower() in {'.py', '.md', '.txt', '.json'} and '__pycache__' not in p.parts)
    sources.update((ROOT / 'installer/path-probe').glob('*.iss'))
    sources.add(ROOT / 'installer/build-tools/Inno7/License.txt')
    evidence = {p for p in (ROOT / 'installer').iterdir()
                if p.is_file() and p.suffix in {'.json', '.xml', '.log'}}
    evidence.add(ROOT / 'installer/corresponding-source/source-manifest.json')
    evidence.add(ROOT / 'installer/payload-050/payload-manifest.json')
    evidence.add(Path(result['source_payload']) / 'payload-manifest.json')
    evidence.update(p for p in (evidence_directory / 'portable-workflow').rglob('*') if p.is_file())
    evidence.update(p for p in (ROOT / 'build').glob('claude-profiles-*') if p.is_file()
                    and p.suffix in {'.xml', '.json', '.log'})
    evidence.add(ROOT / 'build/claude-store-docs-gate.log')
    evidence.add(ROOT / 'build/claude-store-registration-review.json')
    evidence.update(p for p in (ROOT / 'installer/failed-6d5fb15e486c').glob('*')
                    if p.is_file() and p.suffix in {'.json', '.log'})
    # Failed staging folder recorded during the Turkish 0.5.0 acceptance; keep as recorded.
    failed_stage = ROOT / 'installer/staging-çalışma-final'
    evidence.update(p for p in failed_stage.glob('*') if p.is_file() and p.suffix in {'.json', '.log'})
    evidence.update(failed_stage.glob('app/versions/*/deployment-error.json'))
    # Staging folder recorded during the Turkish 0.5.0 acceptance; keep as recorded.
    previous_unicode_stage = ROOT.parent / 'sps-ç'
    if acceptance.parent != previous_unicode_stage:
        evidence.update(p for p in previous_unicode_stage.glob('*') if p.is_file() and p.suffix in {'.json', '.log'})
        evidence.update(previous_unicode_stage.glob('app/versions/*/deployment-error.json'))
    for directory in (build_path.parent, ROOT / 'installer/guardrail-evidence', ROOT / 'installer/guardrail7-evidence',
                      ROOT / 'installer/guardrail7-longpath-red', ROOT / 'installer/guardrail7-fixed-evidence', ROOT / 'installer/path-probe',
                      ROOT / 'installer/portable-worker-prebuild', evidence_directory, acceptance.parent):
        evidence.update(p for p in directory.rglob('*') if p.is_file()
                        and p.suffix.lower() in {'.json', '.jsonl', '.jsonc', '.xml', '.log', '.toml'}
                        and p.stat().st_size <= (64 if p.suffix.lower() == '.log' else 5) * 2**20)
    all_files = sorted(sources | evidence)
    def archive_name(path):
        if path.is_relative_to(ROOT):
            return path.relative_to(ROOT).as_posix()
        if path.is_relative_to(previous_unicode_stage) and acceptance.parent != previous_unicode_stage:
            # Archive path mirrors the recorded sps-ç folder of the Turkish 0.5.0 acceptance; keep as recorded.
            return 'historical-isolated/sps-ç/' + path.relative_to(previous_unicode_stage).as_posix()
        return 'isolated-acceptance/' + path.relative_to(acceptance.parent).as_posix()
    index = {archive_name(p): {'sha256': sha(p), 'bytes': p.stat().st_size}
             for p in all_files}
    destination = build_path.parent / 'SpatialPredictionStudio-0.5.0-installer-source-evidence.zip'
    if destination.exists():
        raise FileExistsError('Do not overwrite a previously archived acceptance')
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in all_files:
            archive.write(path, archive_name(path))
        archive.writestr('archive-manifest.json', json.dumps(index, indent=2))
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip():
            raise IOError('Archive CRC readback failed')
        for name, record in index.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != record['sha256']:
                raise IOError('Archived source/evidence changed: ' + name)
    record = {'status': 'verified', 'archive': str(destination), 'sha256': sha(destination),
              'bytes': destination.stat().st_size, 'files': len(index),
              'installer_sha256': build['sha256'], 'acceptance_sha256': sha(acceptance),
              'scope': 'Installer source and bounded synthetic acceptance evidence; complete application sources are a separate release archive.',
              'excluded': ['Interpreters', 'DLL/EXE binaries', 'TabPFN checkpoint',
                           'Large generated test files', 'Corresponding-source archives already bundled in Setup.exe']}
    (build_path.parent / 'installer-source-evidence-record.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
