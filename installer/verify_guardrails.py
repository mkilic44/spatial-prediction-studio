"""Exercise installer-engine error handling with a tiny, non-product fixture."""
import argparse
import hashlib
import json
from pathlib import Path
from verify_setup import run

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'installer/guardrail7-fixed-evidence')
    parser.add_argument('--stage-name', default='sps-g')
    args = parser.parse_args()
    directory = args.output.resolve()
    if not directory.is_relative_to(ROOT / 'installer') or '/' in args.stage_name or '\\' in args.stage_name or not args.stage_name.startswith('sps-g'):
        raise ValueError('Expected explicit installer evidence and owned short staging paths')
    directory.mkdir(exist_ok=False)
    fixture = ROOT / 'installer/compiler-smoke'
    # Existence marker for the path rule only; never installed or loaded as a DLL.
    pinned_marker = fixture / 'runtime-d/Lib/site-packages/rasterio.libs/libcrypto-3-x64-2c2d24f46e8912647ae707b62db93852.dll'
    pinned_marker.parent.mkdir(parents=True, exist_ok=True)
    pinned_marker.write_bytes(b'non-product path-rule fixture only')
    compiler = ROOT / 'installer/build-tools/Inno7/ISCC.exe'
    common = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-']
    results = []
    for variant, file_list in [('without_python', 'payload-files.iss'),
                               ('uninstall_failure', 'uninstall-payload-files.iss')]:
        output = directory / variant
        output.mkdir()
        stage = ROOT.parent / args.stage_name / variant
        stage.mkdir(parents=True, exist_ok=False)
        command = [str(compiler), '/DPayload=' + str(fixture), '/DOutputFolder=' + str(output),
                   '/DIncludeFile=' + str(fixture / 'build-defines.iss'),
                   '/DPayloadFiles=' + str(fixture / file_list), str(ROOT / 'installer/setup.iss')]
        compiled = run(command, output / 'compile.log', 60)
        assert compiled['exit_code'] == 0
        exe = output / 'SpatialPredictionStudio-0.5.0-Setup.exe'
        if variant == 'without_python':
            report = output / 'preflight.json'
            tests = [
                ('verify_only', ['/VERIFYONLY=1', '/VERIFYREPORT=' + str(report)], 1),
                ('long_rasterio_dll_target', ['/STAGINGROOT=' + str(stage), '/DIR=' + str(stage / ('x' * 90)),
                                      '/COMPONENTS=core,reference_r', '/ACCEPTTABPFNLICENSE=yes'], 7),
                ('wrong_staging_root', ['/STAGINGROOT=' + str(stage / 'allowed'), '/DIR=' + str(stage / 'outside')], 7),
                ('missing_consent', ['/STAGINGROOT=' + str(stage), '/DIR=' + str(stage / 'no-consent'),
                                     '/COMPONENTS=core,reference_r,tabpfn'], 1),
                ('failed_deployment', ['/STAGINGROOT=' + str(stage), '/DIR=' + str(stage / 'failed'),
                                      '/COMPONENTS=core,reference_r,tabpfn', '/ACCEPTTABPFNLICENSE=yes'], 20)]
            for name, arguments, expected in tests:
                result = run([str(exe), *common, *arguments, '/LOG=' + str(output / (name + '.log'))],
                             output / (name + '.stdout.log'), 60)
                assert result['exit_code'] == expected, (name, result)
                results.append({'case': name, **result})
            assert json.loads(report.read_text())['status'] == 'verified_preflight_only'
            assert not (stage / 'outside').exists() and not (stage / 'no-consent').exists() and not (stage / ('x' * 90)).exists()
        else:
            app = stage / 'app'
            installed = run([str(exe), *common, '/STAGINGROOT=' + str(stage), '/DIR=' + str(app),
                             '/COMPONENTS=core,reference_r', '/LOG=' + str(output / 'install.log')], output / 'install.stdout.log', 60)
            assert installed['exit_code'] == 0
            version = app / 'versions/compiler-smoke-not-installable'
            removed = run([str(version / 'unins000.exe'), *common, '/STAGINGROOT=' + str(stage),
                           '/LOG=' + str(output / 'uninstall.log')], output / 'uninstall.stdout.log', 60)
            assert removed['exit_code'] != 0
            assert (version / 'runtime-d/Scripts/python.exe').is_file()
            results.append({'case': 'unregister_failure_preserves_application_files', **removed})
    record = {'status': 'passed', 'fixture_only': True, 'compiler': 'Inno Setup 7.1.0 x64',
              'setup_script_sha256': hashlib.sha256((ROOT / 'installer/setup.iss').read_bytes()).hexdigest(),
              'cases': results, 'real_user_configuration_changed': False}
    (directory / 'readback.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
