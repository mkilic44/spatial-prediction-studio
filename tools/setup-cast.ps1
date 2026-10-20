# Developer setup of the optional R runtime in reference-r\ of this repository: R 4.4.3 from
# reference-r-conda-lock.txt with the pinned micromamba, and the CRAN packages of
# reference-r-packages.csv with their SHA-256 digests checked. Users get the same runtime with
# Install.cmd -WithR.
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
& $Python -I -X utf8 (Join-Path $PSScriptRoot 'cast_reference.py')
if ($LASTEXITCODE -ne 0) { throw 'Could not set up the pinned R and CAST environment.' }
