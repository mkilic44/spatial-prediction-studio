# Temporary diagnostics: console code page, stdin BOM from .NET Framework, Python encodings with ._pth and venv.
$ErrorActionPreference = 'Continue'
$r = New-Object System.Collections.Generic.List[string]
function Add($label, $value) { $r.Add($label + ': ' + (($value | Out-String).Trim())) }
Add 'chcp' (cmd /c chcp)
Add 'PS Console InputEncoding' ([Console]::InputEncoding.WebName + ' preamble=' + [Console]::InputEncoding.GetPreamble().Length)
$pins = Get-Content installer\pins.json -Raw | ConvertFrom-Json
$zip = "$env:RUNNER_TEMP\python.zip"
Invoke-WebRequest $pins.python.url -OutFile $zip -UseBasicParsing
Expand-Archive $zip "$env:RUNNER_TEMP\pyext"
$base = "$env:RUNNER_TEMP\pyext\tools"
$scripts = "$env:RUNNER_TEMP\app\runtime-d\Scripts"
New-Item -ItemType Directory -Force -Path (Split-Path $scripts) | Out-Null
Copy-Item $base $scripts -Recurse
Set-Content -Path "$scripts\python312._pth" -Value "Lib`nDLLs`n.`n../../backend`nimport site`n" -Encoding ASCII -NoNewline
$pth = "$scripts\python.exe"
$code = "import sys,locale,site; print('utf8_mode', sys.flags.utf8_mode, 'isolated', sys.flags.isolated, 'ignore_env', sys.flags.ignore_environment, 'safe_path', sys.flags.safe_path, 'stdout', sys.stdout.encoding, 'stdin', sys.stdin.encoding, 'pref', locale.getpreferredencoding(False), 'usersite', site.ENABLE_USER_SITE)"
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
Add 'pth with env' (& $pth -c $code 2>&1)
Remove-Item env:PYTHONUTF8; Remove-Item env:PYTHONIOENCODING
Add 'pth without env' (& $pth -c $code 2>&1)
Add 'venv create' (& "$base\python.exe" -m venv "$env:RUNNER_TEMP\venvtest" 2>&1)
Add 'venv files' (Get-ChildItem "$env:RUNNER_TEMP\venvtest\Scripts" -Name)
$venv = "$env:RUNNER_TEMP\venvtest\Scripts\python.exe"
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
Add 'venv with env' (& $venv -c $code 2>&1)
Remove-Item env:PYTHONUTF8; Remove-Item env:PYTHONIOENCODING
Add 'venv without env' (& $venv -c $code 2>&1)
Add 'venv pip' (& $venv -m pip --version 2>&1)
Add 'venv prefix' (& $venv -c "import sys; print(sys.prefix, sys.base_prefix, sys.executable); print(sys.path)" 2>&1)
$cs = @'
using System; using System.Diagnostics;
class P { static void Main(string[] a) {
  Console.WriteLine("InputEncoding=" + Console.InputEncoding.WebName + " cp=" + Console.InputEncoding.CodePage + " preamble=" + Console.InputEncoding.GetPreamble().Length);
  var i = new ProcessStartInfo(a[0], "-c \"import sys; print(repr(sys.stdin.buffer.readline()))\"") { UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true, CreateNoWindow = true };
  var p = Process.Start(i); p.StandardInput.WriteLine("token"); p.StandardInput.Close();
  Console.WriteLine("child read: " + p.StandardOutput.ReadToEnd().Trim()); p.WaitForExit(); } }
'@
Set-Content "$env:RUNNER_TEMP\bom.cs" $cs
& C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe /nologo /out:"$env:RUNNER_TEMP\bom.exe" "$env:RUNNER_TEMP\bom.cs" | Out-Null
Add 'BOM test direct' (& "$env:RUNNER_TEMP\bom.exe" $pth 2>&1)
Add 'BOM test via python subprocess' (& $pth -c "import subprocess,sys; r=subprocess.run([sys.argv[1], sys.argv[2]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT); print(r.stdout.decode('utf-8','replace'))" "$env:RUNNER_TEMP\bom.exe" $pth 2>&1)
$msg = ($r -join "`n").Replace('%', '%25').Replace("`r", '%0D').Replace("`n", '%0A')
Write-Output "::notice title=diagnostics::$msg"
