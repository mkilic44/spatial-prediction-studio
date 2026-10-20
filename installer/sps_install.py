"""Install, repair, verify or remove Spatial Prediction Studio for the current Windows user.

Install.cmd runs installer/bootstrap.ps1, which downloads a pinned private CPython from
nuget.org and then runs this script with it. Only the Python standard library is used here.

Layout (the install root defaults to %LOCALAPPDATA%\\Programs\\SpatialPredictionStudio):

    <install root>\\bootstrap-python      private CPython that runs this installer
    <install root>\\cache                 verified downloads (C# compiler, reference assemblies, micromamba)
    <install root>\\logs                  installer logs
    <install root>\\current.json          pointer to the installed version
    <install root>\\<version>\\           application root
        runtime-d\\Scripts\\python.exe     private worker runtime with the pinned packages
        runtime-mcp\\Scripts\\python.exe   optional runtime of the local MCP server
        reference-r\\                    optional R 4.4.3 with CAST 1.0.4 for kNNDM
        backend\\ examples\\ models\\ docs\\ mcp-server\\ installer\\
        package\\SpatialPredictionStudio.esriAddIn
        install-record.json

ArcMap loads the add-in from Documents\\ArcGIS\\AddIns\\Desktop10.8\\{GUID}. Nothing is written
to Program Files, the Windows registry, a system Python or user projects. Results that the panel
writes to %LOCALAPPDATA%\\SpatialPredictionStudio are never touched by this script.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from xml.etree import ElementTree as ET

PRODUCT = 'SpatialPredictionStudio'
GUID = '{DC2D1F95-349B-4F9A-A681-202610170101}'
ADDIN_FILE = 'SpatialPredictionStudio.esriAddIn'
ESRI_TOKEN = '8fc3cc631e44ad86'
MSBUILD_NS = '{http://schemas.microsoft.com/developer/msbuild/2003}'
ADDIN_NS = '{http://schemas.esri.com/Desktop/AddIns}'
# Longest file path inside the pinned Windows wheels (a PyTorch header), measured on 2026-10-20.
LONGEST_PACKAGE_PATH = 119
SITE_PACKAGES = 'runtime-d\\Scripts\\Lib\\site-packages\\'
# Longest file path inside the optional R environment (a GCC header), measured by
# tools/cast_reference.py in the cast-reference workflow on 2026-10-20.
LONGEST_R_PATH = 138
R_PREFIX = 'reference-r\\'
MAX_PATH = 259
REQUIRED_FREE_BYTES = 6 * 1024 ** 3
REQUIRED_FREE_BYTES_R = 2 * 1024 ** 3
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class InstallError(RuntimeError):
    """An expected failure with a message the user can act on."""


class Log:
    def __init__(self, path=None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message):
        line = _dt.datetime.now().strftime('%H:%M:%S') + '  ' + message
        print(line, flush=True)
        if self.path:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def sha512(path):
    digest = hashlib.sha512()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def inside(parent, child):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def remove_tree(path, owner_root):
    """Delete a folder only when it lies inside the folder this installer owns."""
    path = Path(path)
    if not path.exists():
        return
    if not inside(owner_root, path) or Path(path).resolve() == Path(owner_root).resolve():
        raise InstallError('Refusing to delete a folder outside the installation: ' + str(path))
    def retry(function, name, _):
        os.chmod(name, 0o700)
        function(name)
    target = str(path.resolve())
    if sys.platform == 'win32' and not target.startswith('\\\\'):
        # The extended-length form also removes files deeper than MAX_PATH (for example in reference-r).
        target = '\\\\?\\' + target
    shutil.rmtree(target, onerror=retry)


def clean_environment():
    env = os.environ.copy()
    for name in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONUSERBASE', 'PIP_REQUIRE_VIRTUALENV',
                 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER', 'PIP_ROOT', 'CONDA_PREFIX', 'VIRTUAL_ENV'):
        env.pop(name, None)
    env.update({'PYTHONUTF8': '1', 'TABPFN_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
                'HF_HUB_OFFLINE': '1', 'PROJ_NETWORK': 'OFF'})
    return env


def run(command, log, *, cwd=None, timeout=None, capture=False, env=None):
    log('    $ ' + ' '.join('"' + str(part) + '"' if ' ' in str(part) else str(part) for part in command))
    done = subprocess.run([str(part) for part in command], cwd=cwd, env=env or clean_environment(), timeout=timeout,
                          capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    output = (done.stdout or '') + (done.stderr or '')
    if log.path:
        with log.path.open('a', encoding='utf-8') as stream:
            stream.write(output)
    if done.returncode:
        raise InstallError('Command failed with exit code %d: %s\n%s' % (done.returncode, command[0], output[-4000:]))
    return done.stdout if capture else None


# ---------------------------------------------------------------------------------------------
# Downloads


USER_AGENT = 'SpatialPredictionStudio-installer (https://github.com/mkilic44/spatial-prediction-studio)'


def retry_delay(error, attempt):
    """Seconds to wait before the next attempt; servers that limit requests (429, 503) say how long."""
    if isinstance(error, urllib.error.HTTPError) and error.code in (429, 503):
        try:
            return min(max(int(error.headers.get('Retry-After', '')), 1), 120)
        except ValueError:
            return min(15 * 2 ** attempt, 120)
    return 2 ** attempt


def download(url, destination, expected_digest, log, algorithm='sha512', mirrors=()):
    """Download once into the cache and verify the pinned digest (SHA-512 unless stated otherwise).
    mirrors are further URLs of the same file; the digest decides whether a copy is accepted."""
    digest = {'sha512': sha512, 'sha256': sha256}[algorithm]
    label = algorithm.upper().replace('SHA', 'SHA-')
    destination = Path(destination)
    if destination.is_file() and digest(destination) == expected_digest:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.part')
    log('    downloading ' + url)
    sources, last = [url, *mirrors], None
    for attempt in range(6):
        source = sources[attempt % len(sources)]
        try:
            request = urllib.request.Request(source, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as stream:
                shutil.copyfileobj(response, stream, 1 << 20)
            break
        except OSError as error:
            last = error
            time.sleep(retry_delay(error, attempt))
    else:
        raise InstallError('Download failed: %s (%s). Check the internet connection or proxy settings.' % (url, last))
    actual = digest(temporary)
    if actual != expected_digest:
        temporary.unlink()
        raise InstallError('The downloaded file does not match its pinned %s digest: %s' % (label, url))
    os.replace(temporary, destination)
    return destination


def extract_member_tree(archive_path, prefix, destination):
    """Extract the archive members under prefix into destination, keeping relative paths."""
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace('\\', '/')
            if not name.startswith(prefix) or name.endswith('/'):
                continue
            relative = Path(*name[len(prefix):].split('/'))
            if relative.is_absolute() or '..' in relative.parts:
                raise InstallError('Unsafe path in archive: ' + name)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('wb') as out:
                shutil.copyfileobj(source, out)
    return destination


def extract_tar_member(archive_path, member, destination):
    """Extract one regular file from a .tar.bz2 package (conda-forge) to destination."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, 'r:bz2') as archive:
        try:
            info = archive.getmember(member)
        except KeyError:
            raise InstallError('The package does not contain ' + member) from None
        if not info.isfile():
            raise InstallError('Unsafe or unexpected archive member: ' + member)
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.part')
        with archive.extractfile(info) as source, temporary.open('wb') as out:
            shutil.copyfileobj(source, out)
    os.replace(temporary, destination)
    return destination


# ---------------------------------------------------------------------------------------------
# Windows checks


def documents_folder():
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer):
        raise InstallError('The Documents folder cannot be resolved.')
    return Path(buffer.value)


def file_version(path):
    import ctypes
    library = ctypes.windll.version
    size = library.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise InstallError('The version of ArcMap.exe cannot be read.')
    data = ctypes.create_string_buffer(size)
    if not library.GetFileVersionInfoW(str(path), 0, size, data):
        raise InstallError('The version of ArcMap.exe cannot be read.')
    value, length = ctypes.c_void_p(), ctypes.c_uint()
    if not library.VerQueryValueW(data, '\\', ctypes.byref(value), ctypes.byref(length)) or length.value < 52:
        raise InstallError('ArcMap.exe has an invalid version resource.')
    fields = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32))
    return fields[2] >> 16, fields[2] & 65535, fields[3] >> 16, fields[3] & 65535


def arcgis_install_dir():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\ESRI\Desktop10.8', 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
            directory = Path(winreg.QueryValueEx(key, 'InstallDir')[0])
    except OSError:
        raise InstallError('ArcGIS Desktop 10.8 (ArcMap) was not found. Install ArcGIS Desktop 10.8.x first.') from None
    if not (directory / 'bin' / 'ArcMap.exe').is_file():
        raise InstallError('ArcMap.exe was not found in ' + str(directory / 'bin'))
    return directory


def long_paths_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
            return int(winreg.QueryValueEx(key, 'LongPathsEnabled')[0]) == 1
    except (OSError, ImportError):
        return False


def arcmap_running():
    output = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq ArcMap.exe', '/FO', 'CSV', '/NH'],
                            capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return 'arcmap.exe' in output.lower()


def preflight(args, app_root, log):
    """Check the computer before anything is written. Returns facts recorded with the installation."""
    facts = {'python': sys.version.split()[0], 'staging': bool(args.staging)}
    if sys.platform != 'win32' and not args.staging:
        raise InstallError('Spatial Prediction Studio installs on Windows only.')
    if struct.calcsize('P') != 8:
        raise InstallError('The installer must run on 64-bit Python.')
    longest = len(str(app_root)) + 1 + len(SITE_PACKAGES) + LONGEST_PACKAGE_PATH
    if getattr(args, 'with_r', False):
        longest = max(longest, len(str(app_root)) + 1 + len(R_PREFIX) + LONGEST_R_PATH)
    if sys.platform == 'win32' and longest > MAX_PATH and not long_paths_enabled():
        raise InstallError('The installation folder path is too long for Windows (%d > %d characters). '
                           'Run Install.cmd -InstallRoot C:\\SPS or enable long paths in Windows.' % (longest, MAX_PATH))
    free = shutil.disk_usage(app_root.parent if app_root.parent.exists() else Path(app_root.anchor)).free
    required = REQUIRED_FREE_BYTES + (REQUIRED_FREE_BYTES_R if getattr(args, 'with_r', False) else 0)
    if free < required and not args.skip_runtime:
        raise InstallError('At least %d GB of free disk space is required; %.1f GB is free.' % (required // 1024 ** 3, free / 1024 ** 3))
    if args.staging:
        log('    staging mode: ArcMap and .NET checks are skipped; Documents is ' + str(Path(args.staging) / 'Documents'))
        return facts
    import winreg
    directory = arcgis_install_dir()
    version = file_version(directory / 'bin' / 'ArcMap.exe')
    if version[:2] != (10, 8):
        raise InstallError('ArcMap %s is installed; this add-in supports ArcMap 10.8.x.' % '.'.join(map(str, version)))
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
        if int(winreg.QueryValueEx(key, 'Release')[0]) < 528040:
            raise InstallError('.NET Framework 4.8 is required. Install it from Microsoft and run Install.cmd again.')
    if arcmap_running():
        raise InstallError('Close ArcMap and run Install.cmd again.')
    facts.update({'arcgis_dir': str(directory), 'arcmap_version': '.'.join(map(str, version))})
    log('    ArcMap ' + facts['arcmap_version'] + ' found in ' + str(directory))
    return facts


# ---------------------------------------------------------------------------------------------
# Python runtimes


def portable_runtime(base, target, extra_path, log):
    """Copy the private CPython into target/Scripts and isolate it with a ._pth file.

    The layout matches the runtime of the 0.5.0 installer: the worker finds the R runtime and the
    application root relative to sys.executable, which is <root>/runtime-d/Scripts/python.exe.
    A ._pth file does not switch off the user's own site-packages folder (%APPDATA%\\Python), which
    the site module appends. Listing the runtime's site-packages before 'import site' keeps the
    pinned packages ahead of it; the add-in also starts the worker with -s.
    """
    scripts = Path(target) / 'Scripts'
    if Path(target).exists():
        shutil.rmtree(target)
    shutil.copytree(base, scripts, ignore=shutil.ignore_patterns('__pycache__', '*._pth'))
    lines = ['Lib', 'DLLs', '.', 'Lib/site-packages', extra_path, 'import site']
    (scripts / 'python312._pth').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    log('    private Python copied to ' + str(scripts))
    return scripts / 'python.exe'


def pip(python, arguments, log, timeout=3600):
    run([python, '-I', '-X', 'utf8', '-m', 'pip', '--disable-pip-version-check', '--no-input'] + list(arguments),
        log, timeout=timeout)


def install_worker_runtime(base, app_root, source, pins, log):
    python = portable_runtime(base, app_root / 'runtime-d', '../../backend', log)
    common = ['install', '--only-binary=:all:', '--no-warn-script-location']
    log('    installing pinned scientific packages (requirements-lock.txt)')
    pip(python, common + ['-r', source / 'requirements-lock.txt'], log)
    log('    installing pinned deep-learning packages (requirements-d-lock.txt, CPU-only PyTorch)')
    pip(python, common + ['-c', source / 'requirements-lock.txt', '-r', source / 'requirements-d-lock.txt',
                          '--extra-index-url', pins['pytorch_cpu_index']], log)
    pip(python, ['check'], log)
    # The pinned scikit-learn wheel redistributes the unchanged MSVC C++ library. Keeping a copy beside
    # python.exe lets PyTorch load it without a system-wide Visual C++ installation.
    runtime_dll = app_root / 'runtime-d/Scripts/Lib/site-packages/sklearn/.libs/msvcp140.dll'
    if runtime_dll.is_file():
        shutil.copy2(runtime_dll, app_root / 'runtime-d/Scripts/msvcp140.dll')
    return python


def install_mcp_runtime(base, app_root, source, log):
    python = portable_runtime(base, app_root / 'runtime-mcp', '../../mcp-server', log)
    log('    installing the hash-locked MCP packages (requirements-mcp-lock.txt)')
    pip(python, ['install', '--only-binary=:all:', '--no-warn-script-location', '--require-hashes',
                 '-r', source / 'requirements-mcp-lock.txt'], log)
    pip(python, ['check'], log)
    return python


# ---------------------------------------------------------------------------------------------
# Optional R runtime: R 4.4.3 from conda-forge and CAST 1.0.4 from CRAN, used only by kNNDM


def micromamba(cache, pins, log, runtime_dlls=()):
    """Return the pinned micromamba.exe. It needs the 64-bit C++ runtime, which ArcGIS does not always
    install. Copies of the DLLs that come with the private Python runtime (vcruntime140 14.38 from
    CPython, msvcp140 14.40 from scikit-learn) are placed next to it, so the pinned micromamba build
    must not need a newer runtime (see installer/pins.json)."""
    item = pins['micromamba']
    package = download(item['url'], Path(cache) / Path(item['url']).name, item['sha512'], log)
    folder = Path(cache) / ('micromamba-' + item['version'])
    exe = folder / 'micromamba.exe'
    if not exe.is_file():
        extract_tar_member(package, item['member'], exe)
    for dll in runtime_dlls:
        if Path(dll).is_file() and not (folder / Path(dll).name).is_file():
            shutil.copy2(dll, folder / Path(dll).name)
    return exe


def mamba_environment(cache):
    """micromamba without user configuration; its records stay inside the cache folder."""
    env = clean_environment()
    for name in list(env):
        if name.upper().startswith(('CONDA', 'MAMBA', 'R_LIBS')) or name.upper() in ('CONDARC', 'MAMBARC'):
            env.pop(name)
    home = Path(cache) / 'mamba-home'
    home.mkdir(parents=True, exist_ok=True)
    env.update({'MAMBA_ROOT_PREFIX': str(Path(cache) / 'mamba'), 'USERPROFILE': str(home), 'HOME': str(home)})
    return env


def r_environment(prefix):
    """Environment for Rscript of the private R: no user libraries, profiles or site settings."""
    prefix = Path(prefix)
    env = clean_environment()
    for name in list(env):
        if name.upper().startswith(('R_LIBS', 'R_PROFILE', 'R_ENVIRON')):
            env.pop(name)
    env.update({'R_HOME': str(prefix / 'lib/R'), 'LC_ALL': 'en_US.UTF-8', 'LANG': 'en_US.UTF-8'})
    env['PATH'] = os.pathsep.join(map(str, (prefix / 'lib/R/bin/x64', prefix / 'Library/bin', prefix / 'Scripts'))) \
        + os.pathsep + env.get('PATH', '')
    return env


def check_conda_lock(prefix, lock):
    """The environment must hold exactly the packages of the explicit lock file, with the same MD5 sums."""
    expected = {}
    for line in Path(lock).read_text(encoding='utf-8').splitlines():
        if line.startswith('https://'):
            url, _, md5 = line.strip().partition('#')
            expected[url.rsplit('/', 1)[1]] = md5
    found = {}
    for meta in (Path(prefix) / 'conda-meta').glob('*.json'):
        data = read_json(meta)
        found[data.get('fn') or str(data.get('url', '')).rsplit('/', 1)[-1]] = data.get('md5')
    if found != expected:
        missing, extra = sorted(set(expected) - set(found)), sorted(set(found) - set(expected))
        different = sorted(name for name in set(expected) & set(found) if expected[name] != found[name])
        raise InstallError('The R environment does not match reference-r-conda-lock.txt. Missing: %s; extra: %s; '
                           'different MD5: %s' % (missing or 'none', extra or 'none', different or 'none'))
    return len(found)


def create_r_environment(prefix, cache, source, pins, log, runtime_dlls=()):
    """Create the R 4.4.3 environment exactly from reference-r-conda-lock.txt (URLs with MD5 sums)."""
    exe = micromamba(cache, pins, log, runtime_dlls)
    prefix = Path(prefix)
    run([exe, 'create', '--yes', '--no-rc', '--prefix', prefix, '--root-prefix', Path(cache) / 'mamba',
         '--file', Path(source) / 'reference-r-conda-lock.txt'], log, timeout=3600, env=mamba_environment(cache))
    log('    %d conda-forge packages match reference-r-conda-lock.txt'
        % check_conda_lock(prefix, Path(source) / 'reference-r-conda-lock.txt'))
    rscript = prefix / 'lib/R/bin/Rscript.exe'
    if not rscript.is_file():
        raise InstallError('The R environment was created, but Rscript.exe was not found in ' + str(rscript.parent))
    return rscript


CRAN_MIRRORS = ('https://cloud.r-project.org', 'https://cran.r-project.org')


def cran_binaries(package_list, cache, log):
    """Download the CRAN Windows binaries of reference-r-packages.csv and check their SHA-256 digests.
    The main CRAN server is a fallback mirror; the digests decide which copies are accepted."""
    folder = Path(cache) / 'cran'
    with Path(package_list).open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        name = '%s_%s.zip' % (row['Package'], row['Version'])
        url = row['Repository'].rstrip('/') + '/' + name
        mirrors = [url.replace(CRAN_MIRRORS[0], other, 1) for other in CRAN_MIRRORS[1:] if url.startswith(CRAN_MIRRORS[0])]
        download(url, folder / name, row['SHA256'].lower(), log, 'sha256', mirrors)
    return folder


def install_r_runtime(install_root, app_root, source, pins, log):
    """R and the pinned CRAN packages of reference-r-packages.csv in <app>\\reference-r."""
    scripts = app_root / 'runtime-d/Scripts'
    dlls = [scripts / name for name in ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')]
    prefix = app_root / 'reference-r'
    rscript = create_r_environment(prefix, install_root / 'cache', source, pins, log, dlls)
    for name in ('reference-r-packages.csv', 'reference-r-conda-lock.txt'):
        shutil.copy2(source / name, app_root / name)
    log('    downloading CAST 1.0.4 and its dependencies from CRAN (SHA-256 checked)')
    binaries = cran_binaries(source / 'reference-r-packages.csv', install_root / 'cache', log)
    run([rscript, '--vanilla', source / 'tools/install_cast_reference.R', binaries], log, cwd=app_root, timeout=3600,
        env=r_environment(prefix))
    return rscript


# ---------------------------------------------------------------------------------------------
# Application files


def copy_application(source, app_root, with_mcp, log):
    skip = shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache')
    shutil.copytree(source / 'backend', app_root / 'backend', ignore=skip)
    (app_root / 'installer').mkdir(parents=True, exist_ok=True)
    for name in ('verify_runtime.py', 'verify_r.R', 'sps_install.py'):
        shutil.copy2(source / 'installer' / name, app_root / 'installer' / name)
    for name in ('requirements-lock.txt', 'requirements-d-lock.txt', 'requirements-mcp-lock.txt', 'Config.xml', 'README.md', 'LICENSE'):
        shutil.copy2(source / name, app_root / name)
    if (source / 'docs').is_dir():
        shutil.copytree(source / 'docs', app_root / 'docs', ignore=skip)
    if with_mcp:
        shutil.copytree(source / 'mcp-server', app_root / 'mcp-server', ignore=shutil.ignore_patterns('__pycache__', 'tests'))
    (app_root / 'models').mkdir(exist_ok=True)
    log('    application files copied to ' + str(app_root))


# ---------------------------------------------------------------------------------------------
# Add-in build


def project_items(source):
    project = ET.parse(source / 'native/SpatialPredictionStudio/SpatialPredictionStudio.csproj').getroot()
    sources = [source / 'native/SpatialPredictionStudio' / item.get('Include').replace('\\', '/')
               for item in project.iter(MSBUILD_NS + 'Compile')]
    references = [item.get('Include') for item in project.iter(MSBUILD_NS + 'Reference')]
    framework = ['mscorlib'] + [name for name in references if not name.startswith('ESRI.')]
    esri = [name for name in references if name.startswith('ESRI.')]
    return sources, framework, esri


def esri_reference(name, arcgis_dir, override_dir):
    if override_dir:
        candidates = [Path(override_dir) / (name + '.dll')]
    else:
        gac = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET/assembly/GAC_MSIL' / name
        candidates = [gac / ('v4.0_10.8.0.0__' + ESRI_TOKEN) / (name + '.dll')]
        candidates += sorted(gac.glob('v4.0_10.8.*__' + ESRI_TOKEN + '/' + name + '.dll'), reverse=True)
        if arcgis_dir:
            candidates.append(Path(arcgis_dir) / 'bin' / (name + '.dll'))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise InstallError('The ArcObjects assembly %s was not found. Is ArcGIS Desktop 10.8 fully installed?' % name)


def compiler(args, install_root, pins, log):
    """Return the C# compiler command and the .NET Framework 4.8 reference assembly folder."""
    cache = install_root / 'cache'
    if args.reference_assemblies:
        references = Path(args.reference_assemblies)
    else:
        item = pins['net48_reference_assemblies']
        package = download(item['url'], cache / Path(item['url']).name, item['sha512'], log)
        references = cache / ('net48-reference-assemblies-' + item['version'])
        if not (references / 'mscorlib.dll').is_file():
            extract_member_tree(package, item['member_prefix'], references)
    if args.csc:
        # A .py compiler stand-in is used by the installer tests.
        return ([sys.executable, args.csc] if args.csc.endswith('.py') else [args.csc]), references
    item = pins['csharp_compiler']
    package = download(item['url'], cache / Path(item['url']).name, item['sha512'], log)
    folder = cache / ('csharp-compiler-' + item['version'])
    if not (folder / 'csc.exe').is_file():
        extract_member_tree(package, item['member_prefix'], folder)
    return [folder / 'csc.exe'], references


def compile_library(command, references, framework, extra_references, sources, output, log, platform='x86'):
    output.parent.mkdir(parents=True, exist_ok=True)
    arguments = command + ['-nologo', '-noconfig', '-nostdlib+', '-target:library', '-platform:' + platform,
                           '-langversion:7.3', '-optimize+', '-deterministic+', '-debug-', '-out:' + str(output)]
    arguments += ['-reference:' + str(references / (name + '.dll')) for name in framework]
    arguments += ['-reference:' + str(path) for path in extra_references]
    run(arguments + [str(path) for path in sources], log, timeout=600)
    if not output.is_file():
        raise InstallError('The C# compiler did not produce ' + str(output))
    return output


def build_stub_references(command, references, source, folder, esri_names, log):
    """Compile stand-ins for the ArcObjects assemblies (continuous integration and tests only)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    empty = folder / 'Empty.cs'
    empty.write_text('// Intentionally empty stand-in assembly.\n', encoding='utf-8')
    for name in esri_names:
        stub_sources = [source / 'native/ci/ArcObjectsStubs.cs'] if name == 'ESRI.ArcGIS.System' else [empty]
        compile_library(command, references, ['mscorlib', 'System'], [], stub_sources, folder / (name + '.dll'), log, 'anycpu')
    return folder


def icon_png(source):
    spec = importlib.util.spec_from_file_location('sps_build_package', source / 'tools/build_package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.icon_png()


def package_addin(source, app_root, dll, destination, facts):
    config = (source / 'Config.xml').read_bytes()
    version = ET.fromstring(config).find(ADDIN_NS + 'Version').text
    runtime = {'python_executable': str(app_root / 'runtime-d/Scripts/python.exe'),
               'worker_path': str(app_root / 'backend/worker.py'),
               'examples_dir': str(app_root / 'examples'),
               'models_dir': str(app_root / 'models')}
    members = {'Config.xml': config, 'Images/studio.png': icon_png(source),
               'Install/SpatialPredictionStudio.dll': Path(dll).read_bytes(),
               'Install/runtime.json': json.dumps(runtime, ensure_ascii=False, indent=2).encode('utf-8')}
    manifest = {'version': version, 'members': {name: hashlib.sha256(value).hexdigest() for name, value in members.items()},
                'installation': {'installer': 'installer/sps_install.py', 'application_root': str(app_root),
                                 'compiled_on_this_computer': True, 'facts': facts}}
    members['Install/build-manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(members.items()):
            archive.writestr(name, value)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None or json.loads(archive.read('Install/runtime.json')) != runtime:
            raise InstallError('The add-in package failed its read-back check.')
    os.replace(temporary, destination)
    return version, runtime


def addin_target(documents):
    return Path(documents) / 'ArcGIS/AddIns/Desktop10.8' / GUID / ADDIN_FILE


def register_addin(package, documents, app_root):
    target = addin_target(documents)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.is_file():
        backup = app_root / 'previous-addin.esriAddIn'
        shutil.copy2(target, backup)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.new')
    shutil.copy2(package, temporary)
    os.replace(temporary, target)
    if sha256(target) != sha256(package):
        raise InstallError('The installed add-in did not read back correctly: ' + str(target))
    return target, backup


def keep_uninstaller(source, install_root):
    """Keep Uninstall.cmd in the install root, so the downloaded folder can be deleted."""
    folder = install_root / 'installer'
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('bootstrap.ps1', 'sps_install.py', 'pins.json'):
        if (source / 'installer' / name).resolve() != (folder / name).resolve():
            shutil.copy2(source / 'installer' / name, folder / name)
    if (source / 'Uninstall.cmd').resolve() != (install_root / 'Uninstall.cmd').resolve():
        shutil.copy2(source / 'Uninstall.cmd', install_root / 'Uninstall.cmd')


# ---------------------------------------------------------------------------------------------
# Commands


def resolve_documents(args):
    if args.staging:
        return Path(args.staging).resolve() / 'Documents'
    return documents_folder()


def components(args):
    selected = ['core']
    if args.with_mcp:
        selected.append('mcp')
    if args.with_tabpfn:
        selected.append('tabpfn')
    if args.with_r:
        selected.append('reference_r')
    return selected


def install(args):
    source = Path(args.source).resolve()
    install_root = Path(args.install_root).resolve()
    config = ET.parse(source / 'Config.xml').getroot()
    version = config.find(ADDIN_NS + 'Version').text
    app_root = install_root / version
    log = Log(install_root / 'logs' / ('install-' + _dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '.log'))
    pins = read_json(source / 'installer/pins.json')
    selected = components(args)
    if args.with_tabpfn and not args.accept_tabpfn_license:
        raise InstallError('TabPFN weights need -AcceptTabPFNLicense after you have read the Prior Labs License 1.1.')
    if args.mcp_client and not args.with_mcp:
        raise InstallError('MCP clients can be registered only together with -WithMcp.')
    steps = 9
    log('Spatial Prediction Studio %s: installing for this Windows user' % version)
    log('[1/%d] Checking this computer' % steps)
    facts = preflight(args, app_root, log)
    if (install_root / 'current.json').is_file():
        log('    an existing installation was found; it is removed first (your results are kept)')
        uninstall(args, quiet=True)
    if app_root.exists():
        remove_tree(app_root, install_root)
    app_root.mkdir(parents=True)
    log('[2/%d] Copying application files' % steps)
    copy_application(source, app_root, args.with_mcp, log)
    log('[3/%d] Compiling the ArcMap add-in on this computer' % steps)
    command, references = compiler(args, install_root, pins, log)
    sources, framework, esri_names = project_items(source)
    esri_dir = args.esri_reference_dir
    if args.esri_stubs:
        esri_dir = build_stub_references(command, references, source, app_root / 'build/esri-stubs', esri_names, log)
    esri = [esri_reference(name, facts.get('arcgis_dir'), esri_dir) for name in esri_names]
    dll = compile_library(command, references, framework, esri, sources, app_root / 'build/SpatialPredictionStudio.dll', log)
    package = app_root / 'package' / ADDIN_FILE
    addin_version, runtime = package_addin(source, app_root, dll, package, facts)
    base = Path(sys.executable).resolve().parent
    python = None
    if args.skip_runtime:
        log('[4/%d] Python runtime skipped (test mode)' % steps)
    else:
        log('[4/%d] Creating the private Python runtime (downloads about 700 MB; 10-30 minutes)' % steps)
        python = install_worker_runtime(base, app_root, source, pins, log)
        log('    generating the synthetic example data')
        run([python, '-I', '-X', 'utf8', source / 'tools/make_examples.py', '--output', app_root / 'examples'], log, timeout=600)
    log('[5/%d] Installing the add-in for ArcMap' % steps)
    documents = resolve_documents(args)
    target, backup = register_addin(package, documents, app_root)
    record = {'schema_version': 1, 'product': PRODUCT, 'status': 'incomplete', 'version': addin_version,
              'application_root': str(app_root), 'install_root': str(install_root), 'components': selected,
              'addin_path': str(target), 'addin_sha256': sha256(target),
              'previous_addin_backup': str(backup) if backup else None, 'runtime': runtime,
              'mcp_clients': [], 'mcp_registration': None, 'verification': None, 'facts': facts,
              'installed_utc': _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}
    write_json(app_root / 'install-record.json', record)
    write_json(install_root / 'current.json', {'product': PRODUCT, 'version': addin_version, 'application_root': str(app_root)})
    keep_uninstaller(source, install_root)
    log('    add-in installed: ' + str(target))
    if args.with_mcp and not args.skip_runtime:
        log('[6/%d] Installing the local MCP server runtime' % steps)
        mcp_python = install_mcp_runtime(base, app_root, source, log)
        command = [mcp_python, '-I', '-X', 'utf8', app_root / 'mcp-server/register_clients.py', '--root', app_root,
                   '--output-dir', app_root / 'client-configurations']
        if args.mcp_client:
            command.append('--apply')
            for client in args.mcp_client:
                command += ['--client', client]
        record['mcp_clients'] = list(args.mcp_client)
        write_json(app_root / 'install-record.json', record)
        run(command, log, timeout=120)
        record['mcp_registration'] = read_json(app_root / 'client-configurations/registration-result.json')
    else:
        log('[6/%d] Local MCP server not selected' % steps)
    if args.with_tabpfn and not args.skip_runtime:
        log('[7/%d] Downloading the pinned TabPFN v2 regression weights (Prior Labs License 1.1 accepted)' % steps)
        run([python, '-I', '-X', 'utf8', source / 'tools/setup-tabpfn-weights.py', '--destination',
             app_root / 'models/tabpfn-v2'], log, timeout=3600)
    else:
        log('[7/%d] TabPFN weights not selected (the TabPFN model stays unavailable)' % steps)
    if args.with_r and not args.skip_runtime:
        log('[8/%d] Installing the optional R runtime for kNNDM (R 4.4.3, CAST 1.0.4)' % steps)
        install_r_runtime(install_root, app_root, source, pins, log)
    else:
        log('[8/%d] R runtime not selected (kNNDM stays unavailable)' % steps)
    if args.skip_runtime or args.skip_verify:
        log('[9/%d] Runtime verification skipped (test mode)' % steps)
    else:
        log('[9/%d] Verifying the private runtime' % steps)
        output = run([python, '-I', '-X', 'utf8', app_root / 'installer/verify_runtime.py', '--root', app_root,
                      '--components', ','.join(selected)], log, timeout=900, capture=True)
        record['verification'] = json.loads(output.strip().splitlines()[-1])
    record['status'] = 'installed'
    write_json(app_root / 'install-record.json', record)
    log('Installation complete. Start ArcMap; the Spatial Prediction Studio toolbar appears automatically.')
    log('If it does not, open Customize > Toolbars > Spatial Prediction Studio.')
    log('Log file: ' + str(log.path))
    return record


def uninstall(args, quiet=False):
    install_root = Path(args.install_root).resolve()
    log = Log(None if quiet else install_root / 'logs' / ('uninstall-' + _dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '.log'))
    pointer = install_root / 'current.json'
    if not pointer.is_file():
        log('Spatial Prediction Studio is not installed under ' + str(install_root))
        return {'status': 'not_installed'}
    app_root = Path(read_json(pointer)['application_root'])
    if not inside(install_root, app_root):
        raise InstallError('The installation record points outside the install root; nothing was removed.')
    record_path = app_root / 'install-record.json'
    record = read_json(record_path) if record_path.is_file() else {}
    if not args.staging and sys.platform == 'win32' and arcmap_running():
        raise InstallError('Close ArcMap and run Uninstall.cmd again.')
    documents = resolve_documents(args)
    target = addin_target(documents)
    status = {'status': 'removed', 'addin': 'not_present', 'mcp_clients': None}
    if record.get('mcp_clients'):
        python = app_root / 'runtime-mcp/Scripts/python.exe'
        result_file = app_root / 'client-configurations/registration-result.json'
        if python.is_file() and result_file.is_file():
            command = [python, '-I', '-X', 'utf8', app_root / 'mcp-server/register_clients.py', '--root', app_root,
                       '--output-dir', app_root / 'client-removal', '--remove', '--registration-record', result_file]
            for client in record['mcp_clients']:
                command += ['--client', client]
            try:
                run(command, log, timeout=120)
                status['mcp_clients'] = 'removed'
            except InstallError as error:
                status['mcp_clients'] = 'manual_removal_required: ' + str(error)[:300]
    if target.is_file():
        if sha256(target) == record.get('addin_sha256'):
            backup = record.get('previous_addin_backup')
            restored = False
            if backup and Path(backup).is_file():
                try:
                    with zipfile.ZipFile(backup) as archive:
                        old_runtime = json.loads(archive.read('Install/runtime.json'))
                    if all(Path(old_runtime[key]).is_file() for key in ('python_executable', 'worker_path')) \
                            and not inside(app_root, old_runtime['python_executable']):
                        shutil.copy2(backup, target)
                        restored = True
                except (KeyError, ValueError, zipfile.BadZipFile):
                    pass
            if not restored:
                target.unlink()
            status['addin'] = 'previous_restored' if restored else 'removed'
        else:
            status['addin'] = 'kept_because_it_was_changed_after_installation'
    remove_tree(app_root, install_root)
    pointer.unlink()
    log('Spatial Prediction Studio was removed. Your projects and results were not touched.')
    return status


def verify(args):
    install_root = Path(args.install_root).resolve()
    pointer = install_root / 'current.json'
    if not pointer.is_file():
        raise InstallError('Spatial Prediction Studio is not installed under ' + str(install_root))
    app_root = Path(read_json(pointer)['application_root'])
    record = read_json(app_root / 'install-record.json')
    log = Log(None)
    target = addin_target(resolve_documents(args))
    if not target.is_file() or sha256(target) != record['addin_sha256']:
        raise InstallError('The add-in in the ArcMap add-in folder is missing or changed. Run Install.cmd again.')
    python = app_root / 'runtime-d/Scripts/python.exe'
    output = run([python, '-I', '-X', 'utf8', app_root / 'installer/verify_runtime.py', '--root', app_root,
                  '--components', ','.join(record['components'])], log, timeout=900, capture=True)
    result = json.loads(output.strip().splitlines()[-1])
    log('Verification passed: add-in and private runtime are intact.')
    return {'status': 'verified', 'runtime': result['status']}


def parser():
    default_root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Programs' / PRODUCT
    p = argparse.ArgumentParser(description='Spatial Prediction Studio installer')
    p.add_argument('command', choices=('install', 'uninstall', 'verify'))
    p.add_argument('--source', default=str(Path(__file__).resolve().parents[1]), help='repository or release folder')
    p.add_argument('--install-root', default=str(default_root))
    p.add_argument('--with-mcp', action='store_true', help='also install the local MCP server')
    p.add_argument('--mcp-client', action='append', default=[],
                   choices=('codex', 'chatgpt_desktop', 'antigravity', 'claude_desktop', 'claude_code', 'opencode', 'pycharm'))
    p.add_argument('--with-tabpfn', action='store_true', help='download the pinned TabPFN v2 weights')
    p.add_argument('--accept-tabpfn-license', action='store_true')
    p.add_argument('--with-r', action='store_true', help='also install R 4.4.3 with CAST 1.0.4 for kNNDM validation')
    testing = p.add_argument_group('testing and continuous integration')
    testing.add_argument('--staging', help='redirect Documents into this folder and skip the ArcMap checks')
    testing.add_argument('--esri-reference-dir', help='folder with ESRI.ArcGIS.*.dll files to compile against')
    testing.add_argument('--esri-stubs', action='store_true', help='compile against native/ci/ArcObjectsStubs.cs')
    testing.add_argument('--csc', help='C# compiler to use instead of the pinned NuGet compiler')
    testing.add_argument('--reference-assemblies', help='.NET Framework 4.8 reference assembly folder')
    testing.add_argument('--skip-runtime', action='store_true', help='do not create Python runtimes')
    testing.add_argument('--skip-verify', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'install':
            result = install(args)
        elif args.command == 'uninstall':
            result = uninstall(args)
        else:
            result = verify(args)
    except InstallError as error:
        print('\nERROR: ' + str(error), file=sys.stderr, flush=True)
        print('Fix the problem and run Install.cmd again, or run Uninstall.cmd to remove a partial installation.', file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as error:
        print('\nERROR: %s: %s' % (type(error).__name__, error), file=sys.stderr, flush=True)
        print('Fix the problem and run Install.cmd again, or run Uninstall.cmd to remove a partial installation.', file=sys.stderr)
        return 1
    print(json.dumps({'result': result.get('status', 'installed') if isinstance(result, dict) else result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
