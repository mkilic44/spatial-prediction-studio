# Installing Spatial Prediction Studio

Spatial Prediction Studio installs for the current Windows user with one double-click. The installer needs no administrator rights. It does not change Program Files, the Windows registry or any Python you already have.

## Requirements

| Item | Requirement |
|---|---|
| Operating system | Windows 10 or 11, 64-bit |
| ArcGIS | ArcGIS Desktop 10.8.x (ArcMap), installed and licensed |
| .NET Framework | 4.8 or later; the installer checks it |
| Disk space | 6 GB free; the installed application takes about 2.5 GB |
| Internet | Needed for the first installation; about 700 MB is downloaded |
| Rights | A normal user account; no administrator rights |

The analysis itself runs offline after installation.

## Install in three steps

1. **Download.** On the GitHub page of the repository, choose **Code > Download ZIP**, or download a release. Right-click the ZIP file, choose **Properties**, tick **Unblock** if it is shown, then extract the ZIP to a folder.
2. **Close ArcMap and double-click `Install.cmd`.** If Windows shows "Windows protected your PC", choose **More info > Run anyway**. A window shows eight steps. The first installation takes 10 to 30 minutes, mostly to download Python packages.
3. **Start ArcMap.** The **Spatial Prediction Studio** toolbar appears. Click **Spatial Prediction** to open the panel. To try it, open the **1 Project** tab and click **Load example**.

If the toolbar does not appear, open **Customize > Toolbars > Spatial Prediction Studio**.

## What the installer does

1. Checks ArcMap 10.8, .NET Framework 4.8, free disk space and that ArcMap is closed.
2. Copies the application files.
3. Compiles the ArcMap add-in on your computer against your own ArcGIS Desktop assemblies, using a pinned C# compiler.
4. Creates a private Python 3.12.3 runtime and installs the pinned packages from `requirements-lock.txt` and `requirements-d-lock.txt`, including CPU-only PyTorch. It also generates the synthetic example data.
5. Places the add-in where ArcMap looks for add-ins.
6. Optionally installs the local MCP server (see below).
7. Optionally downloads the TabPFN weights (see below).
8. Verifies the runtime: package versions, a small model fit and a raster read-back.

Every download is pinned. The Python runtime, the C# compiler and the .NET reference assemblies come from nuget.org and are checked against SHA-512 digests stored in `installer/pins.json`. Python packages are installed with exact versions from the lock files.

## Where things go

| What | Folder |
|---|---|
| Application, private Python runtimes, examples | `%LOCALAPPDATA%\Programs\SpatialPredictionStudio\<version>` |
| Installer logs and a copy of `Uninstall.cmd` | `%LOCALAPPDATA%\Programs\SpatialPredictionStudio` |
| The add-in that ArcMap loads | `Documents\ArcGIS\AddIns\Desktop10.8\{DC2D1F95-349B-4F9A-A681-202610170101}` |
| Your runs, examples and panel logs (written by the panel) | `%LOCALAPPDATA%\SpatialPredictionStudio` (default output folder; you can choose another one) |

After installation you can delete the downloaded folder.

## Options

Open a Command Prompt in the downloaded folder and add options after `Install.cmd`:

| Option | Effect |
|---|---|
| `-WithMcp` | Also installs the local Model Context Protocol (MCP) server, so AI clients can drive the panel. See [MCP.md](MCP.md). |
| `-McpClient <name>` | Registers the MCP server in a client: `claude_desktop`, `claude_code`, `codex`, `chatgpt_desktop`, `opencode`, `antigravity` or `pycharm`. Repeat for several clients. Needs `-WithMcp`. |
| `-WithTabPFN -AcceptTabPFNLicense` | Downloads the pinned TabPFN v2 regression weights. Read the Prior Labs License 1.1 first; the second option records that you accept it. Without the weights the TabPFN model is unavailable; all other models work. |
| `-InstallRoot <folder>` | Installs somewhere else, for example `C:\SPS` when your user folder path is very long. |

Example:

```
Install.cmd -WithMcp -McpClient claude_desktop
```

## Updating

Download the new version and run `Install.cmd` again. The installer removes the previous version first. Your projects, runs and results are kept.

## Uninstalling

Close ArcMap and double-click `Uninstall.cmd`, either in the downloaded folder or in `%LOCALAPPDATA%\Programs\SpatialPredictionStudio`. It removes the add-in, the application folder, the private runtimes and any MCP client entries it added. It does not touch your projects or the results in your output folders. If you changed the installed add-in file after installation, it is left in place.

## Optional: kNNDM validation (R and CAST)

The k-fold nearest neighbor distance matching (kNNDM) validation design calls CAST 1.0.4 in a pinned R 4.4.3 runtime. `Install.cmd` does not set up this runtime yet: this repository does not include the list of R packages and versions it needs (`reference-r-packages.csv`). Without it, kNNDM stops with the message "The pinned local R/CAST runtime environment was not found." All other validation designs, and the area of applicability, work without R.

## Troubleshooting

| Problem | What to do |
|---|---|
| "Windows protected your PC" | Choose **More info > Run anyway**, or unblock the ZIP before extracting it. |
| A download fails | Check the internet connection. Behind a proxy or firewall, allow `api.nuget.org`, `pypi.org`, `files.pythonhosted.org` and `download.pytorch.org` (and `huggingface.co` for TabPFN). If your network needs a proxy, set the `HTTPS_PROXY` environment variable before running `Install.cmd`. Then run `Install.cmd` again; finished downloads are reused. |
| "The installation folder path is too long" | Run `Install.cmd -InstallRoot C:\SPS`, or enable long paths in Windows. |
| "ArcGIS Desktop 10.8 (ArcMap) was not found" or an ArcObjects assembly is missing | Install or repair ArcGIS Desktop 10.8 including its .NET support. |
| "Close ArcMap" | Close every ArcMap window and run the installer again. |
| The toolbar is missing in ArcMap | Open **Customize > Toolbars** and tick **Spatial Prediction Studio**. Open **Customize > Add-In Manager** and check that Spatial Prediction Studio is listed. If your organization only allows signed add-ins, the add-in security option in the Add-In Manager must allow it. |
| The panel says the Python runtime was not found | Run `Install.cmd` again. To check an installation without changing it, run `powershell -ExecutionPolicy Bypass -File installer\bootstrap.ps1 -Action Verify`. |

Each run writes a log to `%LOCALAPPDATA%\Programs\SpatialPredictionStudio\logs`. Attach it when you report a problem.

## Offline installer for maintainers

The original offline installer (Inno Setup, `installer/setup.iss`) bundles prebuilt runtimes and is built on a maintainer's computer. See [BUILD.md](BUILD.md).
