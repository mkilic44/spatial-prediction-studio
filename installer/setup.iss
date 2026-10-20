#ifndef Payload
  #error Payload define required
#endif
#include IncludeFile
#define RasterioLongestDllRelative "\runtime-d\Lib\site-packages\rasterio.libs\libcrypto-3-x64-2c2d24f46e8912647ae707b62db93852.dll"
#if !FileExists(Payload + RasterioLongestDllRelative)
  #error Recheck the pinned Rasterio DLL path limit before compiling a changed wheel
#endif

[Setup]
AppId=SpatialPredictionStudio-{#BuildId}
AppName=Spatial Prediction Studio
AppVersion=0.5.0
DefaultDirName={localappdata}\Programs\SpatialPredictionStudio
DefaultGroupName=Spatial Prediction Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible and not arm64
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Compression=lzma2/fast
SolidCompression=yes
LZMANumBlockThreads=1
LZMADictionarySize=8192
CompressionThreads=1
OutputDir={#OutputFolder}
OutputBaseFilename=SpatialPredictionStudio-0.5.0-Setup
SetupLogging=yes
CloseApplications=no
RestartApplications=no
DisableProgramGroupPage=yes
UsePreviousTasks=no
UsePreviousAppDir=no
UsePreviousGroup=no
UsePreviousSetupType=no
Uninstallable=yes
CreateUninstallRegKey=not IsStaging
UninstallDisplayName=Spatial Prediction Studio {#BuildId}
UninstallFilesDir={app}\versions\{#BuildId}
LicenseFile={#Payload}\installer\THIRD_PARTY_NOTICE.txt
WizardStyle=modern

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "standard"; Description: "ArcMap ve bilimsel hesaplama"
Name: "custom"; Description: "Bileşenleri seç"; Flags: iscustom

[Components]
Name: "core"; Description: "ArcMap eklentisi ve özel Python ortamı"; Types: standard custom; Flags: fixed
Name: "reference_r"; Description: "Sabit R ve CAST ortamı, karşılık gelen kaynaklarla"; Types: standard
Name: "tabpfn"; Description: "TabPFN model ağırlığı (ayrı lisans onayı)"
#ifdef HasMcp
Name: "mcp"; Description: "Yerel MCP sunucusu ve özel çalışma ortamı"
#endif

[Tasks]
#ifdef HasMcp
Name: "codex"; Description: "Codex istemci kaydını ekle"; Components: mcp; Flags: unchecked
Name: "chatgpt_desktop"; Description: "ChatGPT masaüstü istemci kaydını ekle"; Components: mcp; Flags: unchecked
Name: "antigravity"; Description: "Antigravity istemci kaydını ekle"; Components: mcp; Flags: unchecked
Name: "claude_desktop"; Description: "Claude Desktop istemci kaydını ekle"; Components: mcp; Flags: unchecked
Name: "claude_code"; Description: "Claude Code istemci kaydını ekle"; Components: mcp; Flags: unchecked
Name: "opencode"; Description: "OpenCode istemci kaydını ekle"; Components: mcp; Flags: unchecked
Name: "pycharm"; Description: "PyCharm için içe aktarma dosyasını hazırla"; Components: mcp; Flags: unchecked
#endif

[Files]
#include PayloadFiles
Source: "{#Payload}\models\tabpfn-v2\LICENSE.txt"; Flags: dontcopy

[Code]
var LicensePage: TWizardPage; LicenseMemo: TNewMemo; LicenseAccept: TNewCheckBox; DeploymentFailed: Boolean;

procedure ExitOwnUninstaller(Code: Cardinal);
  external 'ExitProcess@kernel32.dll stdcall';

function IsStaging: Boolean;
begin Result := ExpandConstant('{param:STAGINGROOT|}') <> ''; end;

function VersionRoot: String;
begin Result := ExpandConstant('{app}\versions\{#BuildId}'); end;

function Quote(Value: String): String;
begin Result := '"' + Value + '"'; end;

function DeployArgs(Extra: String): String;
var StageRoot: String;
begin
  Result := '-X utf8 ' + Quote(VersionRoot + '\installer\deploy.py') + ' --root ' + Quote(VersionRoot) + ' ' + Extra;
  StageRoot := ExpandConstant('{param:STAGINGROOT|}');
  if StageRoot <> '' then Result := Result + ' --staging ' + Quote(StageRoot);
end;

function RunDeploy(Extra: String): Boolean;
var Code: Integer;
begin
  Result := Exec(VersionRoot + '\runtime-d\Scripts\python.exe', DeployArgs(Extra), VersionRoot, SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function CheckMachine: Boolean;
var InstallDir: String; Release: Cardinal;
begin
  Result := IsWin64 and RegQueryStringValue(HKLM32, 'SOFTWARE\ESRI\Desktop10.8', 'InstallDir', InstallDir)
    and FileExists(AddBackslash(InstallDir) + 'bin\ArcMap.exe')
    and RegQueryDWordValue(HKLM32, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release)
    and (Release >= 528040);
end;

function InitializeSetup: Boolean;
var ReportPath: String;
begin
  Result := CheckMachine;
  if ExpandConstant('{param:VERIFYONLY|0}') = '1' then begin
    ReportPath := ExpandConstant('{param:VERIFYREPORT|}');
    if ReportPath <> '' then begin
      if Result then SaveStringToFile(ReportPath, '{"status":"verified_preflight_only","writes_application_files":false,"activates_addin":false}', False)
      else SaveStringToFile(ReportPath, '{"status":"failed_preflight","writes_application_files":false,"activates_addin":false}', False);
    end;
    Result := False;
  end else if not Result then SuppressibleMsgBox('64 bit Windows, ArcMap 10.8 ailesi ve .NET Framework 4.8 gerekir.', mbError, MB_OK, IDOK);
end;

procedure InitializeWizard;
var LicenseText: AnsiString;
begin
  LicensePage := CreateCustomPage(wpSelectComponents, 'TabPFN lisansı', 'Ağırlık yalnız bu koşulları kabul ederseniz kurulur.');
  LicenseMemo := TNewMemo.Create(LicensePage); LicenseMemo.Parent := LicensePage.Surface;
  LicenseMemo.Align := alTop; LicenseMemo.Height := LicensePage.SurfaceHeight - ScaleY(40); LicenseMemo.ReadOnly := True; LicenseMemo.ScrollBars := ssVertical;
  ExtractTemporaryFile('LICENSE.txt'); LoadStringFromFile(ExpandConstant('{tmp}\LICENSE.txt'), LicenseText); LicenseMemo.Text := String(LicenseText);
  LicenseAccept := TNewCheckBox.Create(LicensePage); LicenseAccept.Parent := LicensePage.Surface;
  LicenseAccept.Top := LicenseMemo.Height + ScaleY(8); LicenseAccept.Width := LicensePage.SurfaceWidth;
  LicenseAccept.Caption := 'TabPFN ağırlığının lisans koşullarını okudum ve kabul ediyorum.';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin Result := (PageID = LicensePage.ID) and not WizardIsComponentSelected('tabpfn'); end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := (CurPageID <> LicensePage.ID) or LicenseAccept.Checked
    or (WizardSilent and (ExpandConstant('{param:ACCEPTTABPFNLICENSE|no}') = 'yes'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var StageRoot, AppRoot: String;
begin
  Result := '';
  { This pinned Rasterio dependency failed to load at 267 path characters.
    This is a DLL loader check, not a blanket limit on every installed file. }
  if Length(ExpandFileName(VersionRoot + '{#RasterioLongestDllRelative}')) >= 260 then begin
    Result := 'Seçilen kurulum yolu Rasterio kitaplığı için çok uzun. Varsayılan dizini veya daha kısa bir hedefi seçin.';
    Exit;
  end;
  if FileExists(VersionRoot + '\deployment-record.json') then begin
    Result := 'Bu içerik sürümü zaten kurulu. Yeniden kurmak için önce o sürümü kaldırın.';
    Exit;
  end;
  if IsStaging then begin
    StageRoot := AddBackslash(ExpandFileName(ExpandConstant('{param:STAGINGROOT|}')));
    AppRoot := AddBackslash(ExpandFileName(ExpandConstant('{app}')));
    if CompareText(Copy(AppRoot, 1, Length(StageRoot)), StageRoot) <> 0 then begin
      Result := 'Yalıtılmış deneme kurulumu belirtilen STAGINGROOT dizininin içinde olmalıdır.';
      Exit;
    end;
  end;
  if WizardIsComponentSelected('tabpfn') and WizardSilent and (ExpandConstant('{param:ACCEPTTABPFNLICENSE|no}') <> 'yes') then
    Result := 'Sessiz TabPFN kurulumu için /ACCEPTTABPFNLICENSE=yes gerekir.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Args: String;
begin
  if CurStep = ssPostInstall then begin
    Args := '--components ' + Quote(WizardSelectedComponents(False)) + ' --manifest-sha256 {#ManifestHash}';
    if WizardIsComponentSelected('tabpfn') then Args := Args + ' --accept-tabpfn-license';
#ifdef HasMcp
    if WizardIsComponentSelected('mcp') then begin
      if WizardIsTaskSelected('codex') then Args := Args + ' --client codex';
      if WizardIsTaskSelected('chatgpt_desktop') then Args := Args + ' --client chatgpt_desktop';
      if WizardIsTaskSelected('antigravity') then Args := Args + ' --client antigravity';
      if WizardIsTaskSelected('claude_desktop') then Args := Args + ' --client claude_desktop';
      if WizardIsTaskSelected('claude_code') then Args := Args + ' --client claude_code';
      if WizardIsTaskSelected('opencode') then Args := Args + ' --client opencode';
      if WizardIsTaskSelected('pycharm') then Args := Args + ' --client pycharm';
    end;
#endif
    if not RunDeploy(Args) then begin
      DeploymentFailed := True;
      Log('DEPLOYMENT_FAILED: private runtime verification or add-in activation failed.');
      SuppressibleMsgBox('Kurulum doğrulanamadı. Önceki eklenti korunur. deployment-error.json dosyasını inceleyin ve ArcMap kapalıyken yeniden deneyin.', mbError, MB_OK, IDOK);
    end;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin if DeploymentFailed then Result := 20 else Result := 0; end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and DeploymentFailed then begin
    WizardForm.FinishedHeadingLabel.Caption := 'Kurulum doğrulanamadı';
    WizardForm.FinishedLabel.Caption := 'Yeni sürüm etkinleştirilemedi. deployment-error.json dosyasını inceleyin. Önceki eklenti korunur.';
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := RunDeploy('--preflight-only');
  if not Result then SuppressibleMsgBox('Kaldırmadan önce ArcMap uygulamasını kapatın. Eklenti değişmedi.', mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usUninstall) and FileExists(VersionRoot + '\deployment-record.json') then
    if not RunDeploy('--uninstall') then begin
      Log('UNINSTALL_OWNERSHIP_FAILED: application files were retained.');
      SuppressibleMsgBox('Eklenti kaydı güvenle kaldırılamadı. Uygulama dosyaları korunuyor; günlükleri inceleyin.', mbError, MB_OK, IDOK);
      ExitOwnUninstaller(21);
    end;
end;
