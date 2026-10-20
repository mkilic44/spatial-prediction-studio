"""Run only each failed synthetic installation's own uninstaller, preserving evidence."""
import json
from pathlib import Path
import shutil
from verify_setup import run, sha

ROOT = Path(__file__).resolve().parents[1]
BUILD = '0.5.0-6d5fb15e486c'
GUID = '{DC2D1F95-349B-4F9A-A681-202610170101}'


def main():
    records = []
    files = json.loads((ROOT / 'installer/payload-050/payload-manifest.json').read_text())['files']
    # Staging folders recorded during the Turkish 0.5.0 acceptance; keep as recorded.
    for stage in (ROOT / 'installer/staging-çalışma-final', ROOT.parent / 'sps-ç'):
        acceptance = json.loads((stage / 'setup-acceptance.json').read_text())
        if acceptance['status'] != 'failed' or acceptance['build_id'] != BUILD or acceptance['install']['exit_code'] != 20:
            raise ValueError('Refusing to touch a different installation')
        installed = (stage / 'app/versions' / BUILD).resolve()
        if not installed.is_relative_to(stage.resolve()) or (installed / 'deployment-record.json').exists():
            raise ValueError('Expected an unactivated failed synthetic installation')
        error = installed / 'deployment-error.json'
        shutil.copy2(error, stage / 'failed-deployment-error.json')
        target = stage / 'Documents/ArcGIS/AddIns/Desktop10.8' / GUID / 'SpatialPredictionStudio.esriAddIn'
        # Sentinel file name recorded during the Turkish 0.5.0 acceptance; keep as recorded.
        sentinel = stage / 'UserStudies/korunan-çalışma.txt'
        before = {str(p): sha(p) for p in (target, sentinel)}
        result = run([str(installed / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                      '/STAGINGROOT=' + str(stage), '/LOG=' + str(stage / 'failed-uninstall.log')],
                     stage / 'failed-uninstall.stdout.log', 300)
        if result['exit_code'] != 0:
            raise RuntimeError(result)
        assert all(sha(Path(p)) == digest for p, digest in before.items())
        remaining = [name for name in files if (installed / name).exists()]
        assert not remaining, remaining[:10]
        records.append({'stage': str(stage), **result, 'registered_files_removed': len(files),
                        'prior_addin_and_user_study_unchanged': True,
                        'logs_and_failure_records_retained': True})
    record = {'status': 'passed', 'scope': 'Only the two failed, project-owned synthetic installations', 'cases': records}
    (ROOT / 'installer/failed-staging-cleanup.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
