"""Copy passed isolated-installation evidence into the product root without overclaiming UI acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acceptance', type=Path, required=True)
    parser.add_argument('--build-record', type=Path, default=ROOT / 'installer/dist/installer-build-record.json')
    parser.add_argument('--destination', type=Path, default=ROOT / 'installer/final-acceptance')
    args = parser.parse_args()
    source = args.acceptance.resolve()
    if source not in (ROOT.parent / 'sps-ç2/setup-acceptance.json', ROOT.parent / 'sps-cs/setup-acceptance.json'):
        raise ValueError('Expected the authorized final synthetic staging root')
    result = read(source)
    build_path = args.build_record.resolve()
    if not build_path.is_relative_to(ROOT / 'installer'):
        raise ValueError('Expected installer-owned build record')
    build = read(build_path)
    if result['status'] != 'passed' or result['installer_sha256'] != build['sha256'] or sha(Path(build['installer'])) != build['sha256']:
        raise ValueError('No passed acceptance of this exact Setup.exe')
    if build['bytes'] >= 4200000000:
        raise ValueError('The artifact exceeds the single executable size budget')
    signature_path = build_path.parent / 'installer-signature-readback.json'
    signature = read(signature_path)
    if signature['sha256'] != build['sha256'] or Path(signature['path']).resolve() != Path(build['installer']).resolve():
        raise ValueError('The signature readback belongs to a different Setup.exe')
    stage = source.parent
    destination = args.destination.resolve()
    if not destination.is_relative_to(ROOT / 'installer') or destination == ROOT / 'installer':
        raise ValueError('Expected an installer-owned evidence directory')
    if destination.exists():
        previous = read(destination / 'delivery-acceptance.json')
        if previous['acceptance_sha256'] != sha(source) or previous['installer']['sha256'] != build['sha256']:
            raise ValueError('Existing evidence belongs to another acceptance')
        for name, item in previous['mirrored_files'].items():
            if sha(destination / name) != item['sha256']:
                raise ValueError('Existing mirrored evidence changed: ' + name)
    else:
        destination.mkdir()
    paths = {p for p in stage.iterdir() if p.is_file() and p.suffix in {'.json', '.log'}}
    paths.update(stage / name for name in ('relocated-science/readback.json', 'portable-workflow/readback.json'))
    workflow = stage / 'portable-workflow'
    for result_path in (workflow / 'preview/result.json', workflow / 'run/result.json'):
        manifest = read(result_path)
        if manifest['status'] != 'completed':
            raise ValueError('The portable workflow did not complete')
        for item in manifest['outputs']:
            output = (result_path.parent / item['path']).resolve()
            if not output.is_relative_to(result_path.parent.resolve()) or sha(output) != item['sha256']:
                raise ValueError('Portable workflow output changed: ' + item['path'])
    paths.update(p for p in workflow.rglob('*') if p.is_file())
    installed = stage / 'app/versions' / build['build_id']
    paths.update(p for p in installed.glob('*.json') if p.stat().st_size <= 16 * 2**20)
    paths.update(p for p in (installed / 'client-configurations').glob('*.json'))
    paths.update(p for p in (installed / 'client-removal').glob('*.json'))
    for profile in result.get('claude_profile_registration', {}).get('profiles', []):
        for key in ('path', 'backup'):
            if profile.get(key):
                path = Path(profile[key]).resolve()
                if not path.is_relative_to(stage):
                    raise ValueError('Client evidence escaped isolated staging')
                paths.add(path)
    copied = {}
    for path in sorted(paths):
        if path.stat().st_size > 64 * 2**20 and not path.is_relative_to(workflow):
            continue
        relative = path.relative_to(stage)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        digest = sha(path)
        if sha(target) != digest:
            raise IOError('Evidence copy changed: ' + str(path))
        copied[relative.as_posix()] = {'source': str(path), 'sha256': digest, 'bytes': target.stat().st_size}
    summary = {
        'status': 'passed_isolated_installation', 'build_id': build['build_id'],
        'installer': build, 'acceptance_path': str(source), 'acceptance_sha256': sha(source),
        'mirrored_files': copied, 'compiler_resource': read(build_path.parent / 'compiler-resource.json'),
        'authenticode': signature,
        'guardrails': read(ROOT / 'installer/guardrail7-fixed-evidence/readback.json'),
        'failed_staging_cleanup': read(ROOT / 'installer/failed-staging-cleanup.json'),
        'complete_portable_workflow_copied': True,
        'scope': ['Actual silent Setup.exe install and uninstall in a fresh physical staging directory',
                  'Private Python/R dependency paths and exact package versions',
                  'Local offline TabPFN weight fit, eight predictions, save and reload',
                  'CAST reference values and a complete synthetic Ridge workflow',
                  'Seven selected client labels in isolated configuration files; PyCharm manual import only',
                  'Classic and Store Claude profiles registered together and removed by the saved ownership record',
                  'Previous add-in restoration and unrelated configuration/user file preservation'],
        'not_claimed': ['Real ArcMap UI acceptance of this installation', 'Seven live independent client products',
                        'A different physical computer', 'Scientific field validation', 'Code signing'],
    }
    (destination / 'delivery-acceptance.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': summary['status'], 'directory': str(destination), 'files': len(copied)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
