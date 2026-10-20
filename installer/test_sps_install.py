"""Tests of installer/sps_install.py that run on any operating system.

A stand-in compiler records its arguments and writes a placeholder DLL, so these tests check
the compiler call, the add-in package, its registration, reinstall and uninstall without
ArcGIS, Windows or network access. The real compilation is tested by the Windows workflow.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sps_install', ROOT / 'installer/sps_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
VERSION = installer.ET.parse(ROOT / 'Config.xml').getroot().find(installer.ADDIN_NS + 'Version').text

FAKE_CSC = '''import json, sys
from pathlib import Path
args = sys.argv[1:]
out = next(a.split(':', 1)[1] for a in args if a.startswith('-out:'))
Path(out).write_bytes(b'MZ placeholder for ' + Path(out).name.encode())
record = Path(out).with_suffix('.args.json')
record.write_text(json.dumps(args), encoding='utf-8')
'''


@pytest.fixture
def setup(tmp_path):
    csc = tmp_path / 'fake_csc.py'
    csc.write_text(FAKE_CSC, encoding='utf-8')
    references = tmp_path / 'net48'
    references.mkdir()
    esri = tmp_path / 'esri'
    esri.mkdir()
    _, _, names = installer.project_items(ROOT)
    for name in names:
        (esri / (name + '.dll')).write_bytes(b'stand-in')
    stage = tmp_path / 'stage'
    common = ['--source', str(ROOT), '--install-root', str(tmp_path / 'root'), '--staging', str(stage)]
    build = ['--csc', str(csc), '--reference-assemblies', str(references), '--esri-reference-dir', str(esri), '--skip-runtime']
    target = installer.addin_target(stage / 'Documents')
    return {'common': common, 'build': build, 'target': target, 'root': tmp_path / 'root'}


def test_project_items_match_the_csproj():
    sources, framework, esri = installer.project_items(ROOT)
    assert all(path.is_file() for path in sources) and len(sources) == 15
    assert framework[0] == 'mscorlib' and 'System.Windows.Forms' in framework
    assert len(esri) == 12 and 'ESRI.ArcGIS.Desktop.AddIns' in esri


def test_install_builds_registers_and_records(setup):
    assert installer.main(['install'] + setup['common'] + setup['build']) == 0
    app = setup['root'] / VERSION
    record = json.loads((app / 'install-record.json').read_text(encoding='utf-8'))
    assert record['status'] == 'installed' and record['components'] == ['core']
    target = setup['target']
    assert hashlib.sha256(target.read_bytes()).hexdigest() == record['addin_sha256']
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ['Config.xml', 'Images/studio.png', 'Install/SpatialPredictionStudio.dll',
                                              'Install/build-manifest.json', 'Install/runtime.json']
        runtime = json.loads(archive.read('Install/runtime.json'))
        assert archive.read('Config.xml') == (ROOT / 'Config.xml').read_bytes()
    assert runtime == {'python_executable': str(app / 'runtime-d/Scripts/python.exe'), 'worker_path': str(app / 'backend/worker.py'),
                       'examples_dir': str(app / 'examples'), 'models_dir': str(app / 'models')}
    arguments = json.loads((app / 'build/SpatialPredictionStudio.args.json').read_text(encoding='utf-8'))
    assert {'-target:library', '-platform:x86', '-langversion:7.3', '-nostdlib+', '-deterministic+'} <= set(arguments)
    assert sum(a.startswith('-reference:') for a in arguments) == 8 + 12
    assert sum(a.endswith('.cs') for a in arguments) == 15
    assert (app / 'backend/worker.py').is_file() and (app / 'installer/verify_runtime.py').is_file()
    assert json.loads((setup['root'] / 'current.json').read_text(encoding='utf-8'))['application_root'] == str(app)
    # A copy of the uninstaller stays in the install root, so the downloaded folder can be deleted.
    assert (setup['root'] / 'Uninstall.cmd').read_bytes() == (ROOT / 'Uninstall.cmd').read_bytes()
    assert (setup['root'] / 'installer/bootstrap.ps1').is_file() and (setup['root'] / 'installer/sps_install.py').is_file()


def test_reinstall_replaces_the_previous_installation(setup):
    assert installer.main(['install'] + setup['common'] + setup['build']) == 0
    first = json.loads((setup['root'] / VERSION / 'install-record.json').read_text(encoding='utf-8'))
    assert installer.main(['install'] + setup['common'] + setup['build']) == 0
    second = json.loads((setup['root'] / VERSION / 'install-record.json').read_text(encoding='utf-8'))
    assert second['status'] == 'installed' and second['previous_addin_backup'] is None
    assert first['addin_sha256'] == second['addin_sha256']


def test_uninstall_removes_only_owned_files(setup, tmp_path):
    results = tmp_path / 'results-of-the-user'
    results.mkdir()
    (results / 'run.txt').write_text('kept', encoding='utf-8')
    assert installer.main(['install'] + setup['common'] + setup['build']) == 0
    assert installer.main(['uninstall'] + setup['common']) == 0
    assert not setup['target'].exists() and not (setup['root'] / VERSION).exists()
    assert not (setup['root'] / 'current.json').exists() and (setup['root'] / 'logs').is_dir()
    assert (results / 'run.txt').read_text(encoding='utf-8') == 'kept'
    assert installer.main(['uninstall'] + setup['common']) == 0


def test_add_in_changed_after_installation_is_kept(setup):
    assert installer.main(['install'] + setup['common'] + setup['build']) == 0
    setup['target'].write_bytes(b'changed by the user')
    assert installer.main(['uninstall'] + setup['common']) == 0
    assert setup['target'].read_bytes() == b'changed by the user'


def test_previous_add_in_with_a_live_runtime_is_restored(setup, tmp_path):
    other = tmp_path / 'other-installation'
    (other / 'runtime-d/Scripts').mkdir(parents=True)
    (other / 'runtime-d/Scripts/python.exe').write_bytes(b'')
    (other / 'backend').mkdir()
    (other / 'backend/worker.py').write_text('', encoding='utf-8')
    setup['target'].parent.mkdir(parents=True)
    with zipfile.ZipFile(setup['target'], 'w') as archive:
        archive.writestr('Install/runtime.json', json.dumps({'python_executable': str(other / 'runtime-d/Scripts/python.exe'),
                                                             'worker_path': str(other / 'backend/worker.py')}))
    previous = setup['target'].read_bytes()
    assert installer.main(['install'] + setup['common'] + setup['build']) == 0
    assert setup['target'].read_bytes() != previous
    assert installer.main(['uninstall'] + setup['common']) == 0
    assert setup['target'].read_bytes() == previous


def test_options_that_need_consent_or_a_component_are_rejected(setup, capsys):
    assert installer.main(['install', '--with-tabpfn'] + setup['common'] + setup['build']) == 1
    assert 'AcceptTabPFNLicense' in capsys.readouterr().err
    assert installer.main(['install', '--mcp-client', 'claude_desktop'] + setup['common'] + setup['build']) == 1
    assert '-WithMcp' in capsys.readouterr().err
    assert not setup['target'].exists()


def test_portable_runtime_is_isolated_with_a_pth_file(tmp_path):
    base = tmp_path / 'base'
    (base / 'Lib').mkdir(parents=True)
    (base / 'python.exe').write_bytes(b'')
    (base / 'python312._pth').write_text('stale', encoding='utf-8')
    python = installer.portable_runtime(base, tmp_path / 'app/runtime-d', '../../backend', installer.Log(None))
    assert python == tmp_path / 'app/runtime-d/Scripts/python.exe' and python.is_file()
    assert (python.parent / 'python312._pth').read_text(encoding='utf-8').splitlines() == ['Lib', 'DLLs', '.', '../../backend', 'import site']
    # The layout keeps the application root two levels above the interpreter, as the R lookup expects.
    assert python.parents[2] == tmp_path / 'app'


def test_download_checks_the_pinned_digest(tmp_path):
    payload = tmp_path / 'payload.bin'
    payload.write_bytes(b'pinned bytes')
    good = hashlib.sha512(b'pinned bytes').hexdigest()
    target = installer.download(payload.as_uri(), tmp_path / 'cache/payload.bin', good, installer.Log(None))
    assert target.read_bytes() == b'pinned bytes'
    with pytest.raises(installer.InstallError, match='SHA-512'):
        installer.download(payload.as_uri(), tmp_path / 'cache/other.bin', '0' * 128, installer.Log(None))
    assert not (tmp_path / 'cache/other.bin').exists()


def test_archive_extraction_rejects_parent_paths(tmp_path):
    archive = tmp_path / 'package.nupkg'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('tools/python.exe', b'')
        stream.writestr('tools/../../escape.txt', b'')
    with pytest.raises(installer.InstallError, match='Unsafe'):
        installer.extract_member_tree(archive, 'tools/', tmp_path / 'out')
    assert not (tmp_path / 'escape.txt').exists()


def test_pins_are_complete():
    pins = json.loads((ROOT / 'installer/pins.json').read_text(encoding='utf-8'))
    for key in ('python', 'csharp_compiler', 'net48_reference_assemblies'):
        assert pins[key]['url'].startswith('https://api.nuget.org/') and len(pins[key]['sha512']) == 128
    assert pins['python']['version'] == '3.12.3'
    # verify_runtime.py checks the same interpreter version.
    assert 'sys.version_info[:3] == (3, 12, 3)' in (ROOT / 'installer/verify_runtime.py').read_text(encoding='utf-8')
