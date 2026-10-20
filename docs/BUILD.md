# Development, tests and releases

This page is for people who change the code or prepare a release. Users install with `Install.cmd`; see [INSTALL.md](INSTALL.md).

## Repository layout

| Folder | Content |
|---|---|
| `native/SpatialPredictionStudio` | C# add-in: WinForms panel, ArcObjects adapter, job client, MCP pipe bridge (.NET Framework 4.8, x86, C# 7.3) |
| `native/tests` | C# contract tests: `NativeContracts`, `PanelContracts`, `OwnedProcessContracts` |
| `native/ci` | Compile-time stand-ins for the ArcObjects types the add-in uses. Only for builds and tests without ArcGIS; never shipped. |
| `backend` | Python worker (`worker.py`) and the `sps` package; method registry `sps/registry.json` |
| `mcp-server` | Local MCP server and client registration helper |
| `installer` | `Install.cmd` back end (`bootstrap.ps1`, `sps_install.py`, `pins.json`), runtime verification, and the offline Inno Setup installer of the maintainer |
| `tools` | Development scripts: runtime setup, build, registry generation, example data, documentation generators |
| `tests` | Python tests of the backend |
| `build` | Read-back scripts for the acceptance evidence of the Turkish 0.5.0 build (archival) |
| `docs` | User documentation and the terminology list |

## Tests

Linux or Windows, with Python 3.12 and the pinned packages:

```bash
python -m pip install -r requirements-lock.txt
python tools/make_examples.py --output examples
python -m pytest tests installer/test_sps_install.py -q
cd mcp-server && python -m pip install -r <requirements-mcp-lock.txt without pywin32> && python -m pytest tests -q
```

Some tests need parts that are not on every machine:

- R with CAST 1.0.4 and its reference fixtures (`tests/fixtures/cast-1.0.4`): three tests in `test_applicability_c.py`, three in `test_knndm_c.py`, one in `test_engine_c.py`.
- The deep-learning environment (`requirements-d-lock.txt`, PyTorch 2.8.0+cpu) for `test_models_d.py`, `test_model_integration_d.py`, `test_publication_models_d.py`; the TabPFN weights for `test_tabpfn_d.py` and the TabPFN cases.
- Windows for the named-pipe MCP tests and for `installer/test_deploy.py`.
- A built package for `tests/test_package.py`.

## Continuous integration

| Workflow | What it does |
|---|---|
| `.github/workflows/tests.yml` | Ubuntu: backend tests, installer tests and MCP server tests with the pinned packages. The tests listed above that need R/CAST or Windows are deselected by name. |
| `.github/workflows/windows.yml` | Windows: runs the real installer (`installer/bootstrap.ps1`) into a staging folder and compiles the add-in against the ArcObjects stand-ins. It then verifies the runtime and runs the backend tests with the installed runtime, deep-learning models included and TabPFN and R/CAST excluded. Finally it compiles and runs the three C# contract test programs, runs the MCP tests including named pipes, and uninstalls. |

## Building the add-in

`Install.cmd` compiles the add-in on the user's computer with a pinned Roslyn compiler from nuget.org, against the user's own ArcGIS assemblies. To compile with Visual Studio or MSBuild instead, open `native/SpatialPredictionStudio.sln` (Release, x86). The project expects ArcGIS Desktop 10.8 in `C:\Program Files (x86)\ArcGIS\Desktop10.8`.

To check that the C# code compiles without ArcGIS, compile `native/ci/ArcObjectsStubs.cs` into a stand-in `ESRI.ArcGIS.System.dll` and compile the add-in against it. `python installer/sps_install.py install --staging <folder> --esri-stubs --skip-runtime` does this. On Windows it uses the pinned compiler; on other systems pass a C# compiler with `--csc`, for example the `csc` of mono.

## Developer runtime

The scripts in `tools` keep the original development layout inside the repository:

```powershell
tools\setup-runtime.ps1 -BasePython C:\Path\To\python.exe      # runtime\
tools\setup-runtime-d.ps1 -BasePython C:\Path\To\python.exe -RestoreLock   # runtime-d\ from the lock files
tools\setup-runtime-mcp.ps1 -BasePython C:\Path\To\python.exe  # runtime-mcp\
tools\build.ps1                                                # MSBuild, native tests, package, package test
tools\install.ps1                                              # copies dist\SpatialPredictionStudio.esriAddIn into ArcMap
```

`tools\build.ps1` packages `dist\SpatialPredictionStudio.esriAddIn` with a `runtime.json` that points to the repository's `runtime-d`. That package is for the developer's own computer. `tools/setup-cast.ps1` sets up the optional R and CAST runtime with Conda; it needs `reference-r-packages.csv`, which is not yet in this repository.

## Documentation

- `docs/METHODS.md` is generated from the method registry: `python tools/make_methods_doc.py`. A test fails when it is out of date.
- `docs/THIRD_PARTY_LICENSES.md` is generated from the Windows wheels of the pinned versions: `python tools/make_third_party_doc.py --wheels <folder>`.
- `docs/TERMINOLOGY.md` fixes one English term for each Turkish term of the original interface. Use it for any new interface or report text.

## Pinned downloads

`installer/pins.json` stores the URL and SHA-512 digest of the CPython, Roslyn and .NET reference assembly packages from nuget.org. Each digest matches the `packageHash` that nuget.org publishes for the package. `sps_install.py` stores the longest file path in the pinned wheels (`LONGEST_PACKAGE_PATH`) to check the Windows path limit; measure it again when the lock files change.

## Releasing

1. Update the version number in `Config.xml`, `native/SpatialPredictionStudio/EntryPoints.cs`, `backend/sps/__init__.py`, `mcp-server/server.py`, `mcp-server/sps_mcp/__init__.py`, `installer/setup.iss`, `installer/prepare_payload.py`, `tools/build_installer.py` and `tests/test_package.py`. The scripts in `build/` and the archived acceptance scripts in `installer/` refer to the recorded 0.5.0 build and keep that number.
2. Regenerate `docs/METHODS.md` and, if the lock files changed, `docs/THIRD_PARTY_LICENSES.md`.
3. Make sure both workflows pass.
4. Tag the commit (for example `v0.5.1`) and create a GitHub release. The source archive of the release is what users download and install with `Install.cmd`.

The offline installer (`installer/setup.iss`, `installer/prepare_payload.py`, `tools/build_installer.py`) bundles prebuilt runtimes so that a computer without internet access can install. It is built on the maintainer's computer from local runtimes, the R environment and the corresponding sources.

## Archived acceptance scripts

The scripts in `build/` re-read the recorded acceptance evidence of the Turkish 0.5.0 build (fixed run IDs and package digests). Some of their string literals stay in Turkish because they must match the recorded logs and reports; each one is marked with a comment. They are not part of the normal build.
