<#
.SYNOPSIS
    Builds the one-click replay demo of the STM bus digital twin, for Windows or (-Web) for a web browser.

.DESCRIPTION
    Exports the Godot project with the "STM Demo" preset (replay mode only: no RabbitMQ,
    MongoDB, Docker or network needed) and zips the result:
      visualization\Build\Demo\STM DT Demo.exe   + data_* folder (.NET runtime) + replays\ folder
                                                + "Start in safe mode.bat" (OpenGL, Minimum quality)
      visualization\Build\STM-DT-Demo.zip       share this: unzip, double-click "STM DT Demo.exe"
    Every *.json replay in visualization\replays is bundled inside the exe.
    More replays can be dropped later into the replays\ folder next to the exe (no rebuild).

    With -Web: exports the "Web" preset with the standard Godot (a web page runs no C#: replays only):
      visualization\Build\Web\index.html + index.pck, index.wasm...   a static site: GitHub Pages publishes it
                                                                     (.github\workflows\web.yml builds it the same way)

    Before it is shared, the build is checked (software\tools\check_export.py, with the repository's
    .venv Python, else Python 3.10 or newer on the PATH): when STM_API_KEY is set on this computer, no
    file of the build or of the project may contain its value, and neither the pack nor the project's
    files may hold a secret-like file or API-key wording.

.PARAMETER Godot
    Path to the Godot 4.7 .NET (mono) editor (with -Web: the standard Godot 4.7, not .NET), preferably
    the *_console.exe.
    Default: $env:GODOT_BIN (with -Web: $env:GODOT_WEB_BIN), else the newest matching
    Godot_v4.7*mono*console.exe (with -Web: Godot_v4.7*console.exe without "mono") found on the
    Desktop, in Downloads, in Program Files or on the PATH.

.PARAMETER Web
    Builds the web version (visualization\Build\Web) instead of the Windows demo.

.PARAMETER NoPause
    Never wait for a key press at the end (for scripts and automation).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_demo.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_demo.ps1 -Godot "D:\Godot\Godot_v4.7.1-stable_mono_win64_console.exe" -NoPause
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_demo.ps1 -Web
#>
param(
    [string]$Godot = "",
    [switch]$Web,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$Project = Join-Path $Root "visualization"
$Preset = if ($Web) { "Web" } else { "STM Demo" }
$BuildDir = Join-Path $Project "Build"
$DemoDir = Join-Path $BuildDir "Demo"
$ExePath = Join-Path $DemoDir "STM DT Demo.exe"
$ZipPath = Join-Path $BuildDir "STM-DT-Demo.zip"
$WebDir = Join-Path $BuildDir "Web"
$OutDir = if ($Web) { $WebDir } else { $DemoDir }
$OutPath = if ($Web) { Join-Path $WebDir "index.html" } else { $ExePath }
$LogPath = Join-Path $BuildDir $(if ($Web) { "build_web.log" } else { "build_demo.log" })
$ReplaySource = Join-Path $Project "replays"
$Checker = Join-Path $Root "software\tools\check_export.py"
$MapPath = Join-Path $Project "assets\map not joined.glb"
# The Godot edition: .NET for the Windows demo (the live feed's listener is C#), the standard one for the web (the
# web export has no C#)
$Edition = if ($Web) { "" } else { " .NET" }
$ExeEnding = if ($Web) { "win64_console.exe" } else { "mono_win64_console.exe" }
$GodotVariable = if ($Web) { "GODOT_WEB_BIN" } else { "GODOT_BIN" }

$ReplaysReadme = @'
# Extra replays

Copy more replay files (`*.json`, format `stm-dt-replay`) into this folder, then start
`STM DT Demo.exe` again. No rebuild is needed.

- Files from this folder are listed first in the replay menu (marked "(file)"), newest
  recording first (by the start time inside the file), and the first one that loads plays at
  startup. Titles do not change the order; replays with the same start time are sorted by file
  name. The replays built into the exe come after them and stay available.
- A file that cannot be read is skipped with a message.
- The 3D map covers downtown Montreal only: buses outside it float over empty ground.
  Right-click for free-fly (WASD to move) to reach them.
- Click a bus to see its occupancy chart and the stops of its line. A replay may also carry
  precomputed occupancy forecasts (key "forecast"); the chart then shows them after the observed
  occupancy. Replays without one show the observed occupancy only. A forecast in the older
  format (version 1) is ignored with a message: run the forecast command on it again.
- The "Lines" buttons at the top left show or hide each line (double-click: only that line).
- Make a replay with the project's recording tool, for example (from the repository root):
  `python software\recording export recordings\<recording>.sqlite --minutes 60 --name "My hour"`
  and add forecasts to it with the Python of the forecast environment (.venv-forecast, with Timestral):
  `.venv-forecast\Scripts\python software\recording forecast <replay>.json`
  See "Record & replay demo" in the project README.
'@

# For a computer where the demo does not start (no working Vulkan / Direct3D 12 driver) or is too slow even at a low
# quality: the OpenGL renderer and the Minimum preset, for this run only (nothing is saved)
$SafeModeBat = @'
@echo off
rem Safe mode: the OpenGL renderer and the Minimum display quality, for this run only.
rem Use it when "STM DT Demo.exe" does not start or is too slow. Nothing is saved.
start "" "%~dp0STM DT Demo.exe" --rendering-driver opengl3 --rendering-method gl_compatibility -- --quality=minimum
'@

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Log([string[]]$Lines) {
    Add-Content -LiteralPath $LogPath -Value $Lines -Encoding UTF8
}

# True when this window was opened by Explorer (right-click > Run with PowerShell): it closes at the end
function Test-StartedFromExplorer {
    try {
        $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
        return (Get-Process -Id $parentId -ErrorAction Stop).ProcessName -eq "explorer"
    } catch {
        return $false
    }
}

# Godot version the C# project is made for, e.g. "4.7.1" from <Project Sdk="Godot.NET.Sdk/4.7.1">
function Get-ProjectGodotVersion {
    $csproj = Get-ChildItem -LiteralPath $Project -File | Where-Object { $_.Extension -eq ".csproj" } | Select-Object -First 1
    if ($csproj) {
        $match = [regex]::Match((Get-Content -LiteralPath $csproj.FullName -Raw), 'Godot\.NET\.Sdk/(\d+\.\d+(?:\.\d+)?)')
        if ($match.Success) { return $match.Groups[1].Value }
    }
    return "4.7"
}

# "4.7.1" -> [version] 4.7.1 ; "4.7" -> 4.7
function ConvertTo-Version([string]$Text) {
    $parts = $Text.Split(".")
    if ($parts.Count -eq 1) { $Text += ".0" }
    return [version]$Text
}

# Version written in a Godot exe name, e.g. Godot_v4.7.1-stable_mono_win64_console.exe -> 4.7.1
function Get-ExeVersion([string]$Name) {
    $match = [regex]::Match($Name, '^Godot_v(\d+\.\d+(?:\.\d+)?)-')
    if ($match.Success) { return ConvertTo-Version $match.Groups[1].Value }
    return $null
}

# Prefer the *_console.exe next to a GUI Godot exe: it waits for the export and shows its output
function Resolve-ConsoleExe([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    if ($item.BaseName -notlike "*_console") {
        $console = Join-Path $item.DirectoryName ($item.BaseName + "_console.exe")
        if (Test-Path -LiteralPath $console) { return $console }
    }
    return $item.FullName
}

function Find-Godot([string]$Wanted) {
    if ($Godot -ne "") {
        if (-not (Test-Path -LiteralPath $Godot -PathType Leaf)) { throw "Godot not found at -Godot `"$Godot`"." }
        return Resolve-ConsoleExe $Godot
    }
    $fromVariable = [Environment]::GetEnvironmentVariable($GodotVariable)
    if ($fromVariable) {
        if (-not (Test-Path -LiteralPath $fromVariable -PathType Leaf)) { throw "$GodotVariable points to a missing file: $fromVariable" }
        return Resolve-ConsoleExe $fromVariable
    }

    $roots = @(
        @{ Path = [Environment]::GetFolderPath("Desktop"); Depth = 3 },
        @{ Path = (Join-Path $HOME "Desktop"); Depth = 3 },
        @{ Path = (Join-Path $HOME "OneDrive\Desktop"); Depth = 3 },
        @{ Path = (Join-Path $HOME "Downloads"); Depth = 3 },
        @{ Path = $env:ProgramFiles; Depth = 2 },
        @{ Path = ${env:ProgramFiles(x86)}; Depth = 2 },
        @{ Path = (Join-Path $env:LOCALAPPDATA "Programs"); Depth = 2 }
    )
    $seen = @{}
    $found = @()
    foreach ($r in $roots) {
        if (-not $r.Path -or -not (Test-Path -LiteralPath $r.Path) -or $seen.ContainsKey($r.Path.ToLower())) { continue }
        $seen[$r.Path.ToLower()] = $true
        $found += @(Get-ChildItem -LiteralPath $r.Path -Recurse -Depth $r.Depth -File -Filter "Godot_v*_console.exe" -ErrorAction SilentlyContinue |
            Where-Object { ($_.Name -like "*mono*") -ne [bool]$Web -and (Get-ExeVersion $_.Name) })
    }
    # A Godot folder added to the PATH
    $found += @(Get-Command -CommandType Application -Name "Godot_v*_console.exe" -ErrorAction SilentlyContinue |
        ForEach-Object { Get-Item -LiteralPath $_.Source } |
        Where-Object { ($_.Name -like "*mono*") -ne [bool]$Web -and (Get-ExeVersion $_.Name) })
    $found = @($found | Sort-Object FullName -Unique)

    # Same major.minor as the project only: another version would migrate project.godot and the .csproj
    $wantedVersion = ConvertTo-Version $Wanted
    $usable = @($found | Where-Object {
            $v = Get-ExeVersion $_.Name
            $v.Major -eq $wantedVersion.Major -and $v.Minor -eq $wantedVersion.Minor
        } | Sort-Object @{ Expression = { Get-ExeVersion $_.Name }; Descending = $true },
                        @{ Expression = { $_.Name -like "*-stable_*" }; Descending = $true },
                        @{ Expression = { $_.LastWriteTime }; Descending = $true })
    if ($usable.Count -eq 0) {
        $others = ($found | ForEach-Object { "    " + $_.FullName }) -join "`n"
        if ($others) { $others = "`n  Found other versions:`n$others" }
        $exe = "C:\path\to\Godot_v$Wanted-stable_$ExeEnding"
        $webArgument = if ($Web) { " -Web" } else { "" }
        $webFlag = if ($Web) { " --web" } else { "" }
        throw ("Could not find the Godot $Wanted$Edition editor (Godot_v$Wanted-stable_$ExeEnding).$others`n" +
            "  Download Godot $Wanted$Edition from https://godotengine.org/download/archive/ , unzip it on the Desktop,`n" +
            "  or tell this script where it is, in one of these ways:`n" +
            "    powershell -ExecutionPolicy Bypass -File build_demo.ps1$webArgument -Godot `"$exe`"`n" +
            "    setx $GodotVariable `"$exe`"   (then open a new window, or double-click build_demo.bat again)`n" +
            "    `$env:$GodotVariable = `"$exe`"; .\build_demo.bat$webFlag --no-pause   (PowerShell, this window only)")
    }
    # Exact version first (e.g. 4.7.1 for Godot.NET.Sdk/4.7.1), else the newest 4.7.x
    $exact = @($usable | Where-Object { (Get-ExeVersion $_.Name) -eq $wantedVersion })
    if ($exact.Count -gt 0) { return $exact[0].FullName }
    return $usable[0].FullName
}

# Runs Godot, shows its output (without color codes), appends it to the log; returns the exit code
function Invoke-Godot([string[]]$Arguments) {
    return Invoke-Logged $GodotExe $Arguments
}

# Runs a program, shows its output (without color codes), appends it to the log; returns the exit code
function Invoke-Logged([string]$Exe, [string[]]$Arguments) {
    Write-Log @("", "> `"$Exe`" " + (($Arguments | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "))
    $ansi = [regex]("$([char]27)" + '\[[0-9;]*[A-Za-z]')
    $lines = New-Object System.Collections.Generic.List[string]
    $previous = $ErrorActionPreference
    # Godot prints warnings on stderr; success is judged by the exit code and the files produced
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $line = $ansi.Replace([string]$_, "")
            $lines.Add($line)
            Write-Host "   $line"
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    Write-Log $lines.ToArray()
    $script:GodotOutput = $lines
    return $code
}

# A Python 3.10+ to run software\tools\check_export.py: the repository's .venv, else py -3, python or python3 on the
# PATH (the Microsoft Store's "python" placeholder fails the test). Returns @(exe, arguments...) or $null
function Find-Python {
    $candidates = @(@((Join-Path $Root ".venv\Scripts\python.exe")), @("py", "-3"), @("python"), @("python3"))
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $rest = @($candidate | Select-Object -Skip 1)
            & $exe @rest -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return ,$candidate } # The comma keeps a one-item array an array
        } catch {
        } finally {
            $ErrorActionPreference = $previous
        }
    }
    return $null
}

# Reads "name" from the start of a replay file; returns $null when it does not look like a replay
function Get-ReplayHeader([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $buffer = New-Object byte[] 8192
        $count = $stream.Read($buffer, 0, $buffer.Length)
    } finally {
        $stream.Close()
    }
    $head = [Text.Encoding]::UTF8.GetString($buffer, 0, $count)
    if ($head -notmatch '"format"\s*:\s*"stm-dt-replay"') { return $null }
    $name = [regex]::Match($head, '"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
    if ($name.Success) { return $name.Groups[1].Value }
    return [IO.Path]::GetFileNameWithoutExtension($Path)
}

# True when a replay carries precomputed occupancy forecasts (key "forecast", written after "frames")
function Test-ReplayForecast([string]$Path) {
    return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8).Contains('"forecast":')
}

$exitCode = 0
$started = Get-Date
try {
    Write-Host $(if ($Web) { "STM digital twin - web version build" } else { "STM digital twin - one-click demo build" }) -ForegroundColor White
    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
    # Godot must not import the builds: their images and .json files would end up in the next export
    $gdIgnore = Join-Path $BuildDir ".gdignore"
    if (-not (Test-Path -LiteralPath $gdIgnore)) { [IO.File]::WriteAllText($gdIgnore, "") }
    Set-Content -LiteralPath $LogPath -Value "build_demo.ps1$(if ($Web) { ' -Web' }) started $($started.ToString('yyyy-MM-dd HH:mm:ss'))" -Encoding UTF8
    $steps = if ($Web) { 4 } else { 6 }

    Write-Step "1/$steps Checking tools and replays"
    if (-not (Test-Path -LiteralPath (Join-Path $Project "project.godot"))) { throw "No Godot project at $Project" }
    # A clone without Git LFS has a small text pointer instead of the map: it would export an empty city
    if (-not (Test-Path -LiteralPath $MapPath) -or (Get-Item -LiteralPath $MapPath).Length -lt 1MB) {
        throw "$MapPath is missing or is a Git LFS pointer, not the map. Install Git LFS, then run: git lfs pull"
    }
    $wanted = Get-ProjectGodotVersion
    $GodotExe = Find-Godot $wanted
    Write-Host "   Godot      : $GodotExe"
    $versionText = ((& $GodotExe --version) | Select-Object -Last 1)
    if ($versionText) { $versionText = $versionText.Trim() }
    Write-Host "   Version    : $versionText (project made for $wanted)"
    if ($Web -and $versionText -match '\.mono') {
        throw "This Godot is the .NET (mono) edition, which cannot export to the web: get the standard Godot $wanted (not .NET)."
    }
    if (-not $Web -and $versionText -notmatch '\.mono') {
        throw "This Godot is not the .NET (mono) edition. The project uses C#: get Godot $wanted .NET."
    }
    $versionMatch = [regex]::Match($versionText, '^(\d+)\.(\d+)')
    $wantedVersion = ConvertTo-Version $wanted
    if (-not $versionMatch.Success -or [int]$versionMatch.Groups[1].Value -ne $wantedVersion.Major -or [int]$versionMatch.Groups[2].Value -ne $wantedVersion.Minor) {
        throw "Godot $versionText cannot build this project (made for $wanted): it would migrate project.godot. Use Godot $wanted$Edition."
    }
    $templateMatch = [regex]::Match($versionText, '^(\d+\.\d+(?:\.\d+)?\.[A-Za-z0-9]+(?:\.mono)?)')
    # Web: single-threaded (GitHub Pages cannot send the headers a multi-threaded page needs)
    $templateFile = if ($Web) { "web_nothreads_release.zip" } else { "windows_release_x86_64.exe" }
    if ($templateMatch.Success) {
        $templateName = $templateMatch.Groups[1].Value
        $templateDirs = @(
            (Join-Path $env:APPDATA "Godot\export_templates\$templateName"),
            (Join-Path (Split-Path -Parent $GodotExe) "editor_data\export_templates\$templateName")
        )
        $hasTemplates = @($templateDirs | Where-Object { Test-Path -LiteralPath (Join-Path $_ $templateFile) }).Count -gt 0
        if (-not $hasTemplates) {
            throw ("Export templates $templateName are not installed.`n" +
                "  Open the project in Godot, then Editor > Manage Export Templates > Download and Install.")
        }
        Write-Host "   Templates  : $templateName"
    }
    # The .NET runtime installs dotnet.exe too: only "dotnet --list-sdks" tells if an SDK is there (the web needs none)
    if (-not $Web) {
        $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
        $sdks = @()
        if ($dotnet) {
            $previous = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                $sdks = @(& $dotnet.Source --list-sdks 2>$null | ForEach-Object { [string]$_ } | Where-Object { $_ -match '^(\d+)\.' -and [int]$Matches[1] -ge 8 })
            } finally {
                $ErrorActionPreference = $previous
            }
        }
        if ($sdks.Count -eq 0) {
            $what = if ($dotnet) { "No .NET SDK 8 or newer in $($dotnet.Source) (only the .NET runtime or an older SDK)" } else { "The .NET SDK is missing" }
            throw "$what. It is needed to build the C# scripts: install .NET SDK 8 or newer from https://dotnet.microsoft.com/download"
        }
        Write-Host "   .NET SDK   : $(($sdks[-1] -split ' ')[0]) ($($dotnet.Source))"
    }
    $python = Find-Python
    if ($python) {
        Write-Host "   Python     : $($python -join ' ') (checks the build)"
    } elseif ($env:STM_API_KEY) {
        throw "STM_API_KEY is set on this computer, but no Python 3.10 or newer was found to check that the build does not contain it. Install it, or the repository's .venv (see README)."
    } else {
        Write-Warning "No Python 3.10+ found: the build will not be checked for secrets (software\tools\check_export.py)."
    }

    $replays = @(Get-ChildItem -LiteralPath $ReplaySource -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -eq ".json" } | Sort-Object Name)
    if ($replays.Count -eq 0) {
        Write-Warning "No replay in $ReplaySource : the demo will start without buses. Export one first (see README, 'Record & replay demo')."
    }
    foreach ($replay in $replays) {
        $name = Get-ReplayHeader $replay.FullName
        if ($null -eq $name) {
            Write-Warning "$($replay.Name) is not an stm-dt-replay file: the demo will skip it."
            continue
        }
        Write-Host ("   Replay     : {0} ({1:N1} MB) - {2}" -f $replay.Name, ($replay.Length / 1MB), $name)
        if (-not (Test-ReplayForecast $replay.FullName)) {
            # Not an error: the bus chart then shows the observed occupancy only
            Write-Host "   Note       : no occupancy forecast in it (the bus chart shows history only). To add one:" -ForegroundColor Yellow
            Write-Host "                .venv-forecast\Scripts\python software\recording forecast `"visualization\replays\$($replay.Name)`"" -ForegroundColor Yellow
            Write-Host "                (from the repo root; set up .venv-forecast first, see README > Occupancy colors, chart and forecasts)" -ForegroundColor Yellow
        }
    }

    # No long-lived .NET build servers for this build (this process and Godot only): the C# compiler
    # server started by "dotnet publish" inherits Godot's output pipe and would keep this script
    # waiting for ~10 minutes after the export is done
    $env:UseSharedCompilation = "false"
    $env:MSBUILDDISABLENODEREUSE = "1"
    $env:DOTNET_CLI_USE_MSBUILD_SERVER = "0"

    Write-Step "2/$steps Importing the project (assets, script caches)"
    $code = Invoke-Godot @("--headless", "--path", $Project, "--import")
    if ($code -ne 0) { throw "Godot --import failed with exit code $code (see $LogPath)." }

    Write-Step "3/$steps Exporting preset `"$Preset`" ($(if ($Web) { 'pack' } else { 'C# build + pack' }), takes a minute or two)"
    if (Test-Path -LiteralPath $OutDir) {
        try {
            Remove-Item -LiteralPath $OutDir -Recurse -Force
        } catch {
            $inUse = if ($Web) { "is a file of it open, or served?" } else { "is `"STM DT Demo.exe`" still running?" }
            throw "Cannot delete the previous build in $OutDir ($inUse): $($_.Exception.Message)"
        }
    }
    # Also the previous zip: a failed build must not leave an outdated zip that looks shareable
    if (-not $Web -and (Test-Path -LiteralPath $ZipPath)) {
        try {
            Remove-Item -LiteralPath $ZipPath -Force
        } catch {
            throw "Cannot delete the previous $ZipPath (is it open in another program?): $($_.Exception.Message)"
        }
    }
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $code = Invoke-Godot @("--headless", "--path", $Project, "--export-release", $Preset, $OutPath)
    if ($code -ne 0) { throw "Godot export failed with exit code $code (see $LogPath)." }
    if ($Web) {
        foreach ($name in @("index.html", "index.js", "index.wasm", "index.pck")) {
            if (-not (Test-Path -LiteralPath (Join-Path $WebDir $name) -PathType Leaf)) { throw "Godot finished but $name was not created in $WebDir (see $LogPath)." }
        }
    } else {
        if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) { throw "Godot finished but $ExePath was not created (see $LogPath)." }
        $dataDirs = @(Get-ChildItem -LiteralPath $DemoDir -Directory -Filter "data_*")
        if ($dataDirs.Count -eq 0) { throw "Godot finished but the .NET data_* folder is missing in $DemoDir (see $LogPath)." }
    }
    $stored = @($script:GodotOutput | Where-Object { $_ -match 'Storing File: ' })
    $packed = @($stored | Where-Object { $_ -match 'Storing File: res://replays/.+\.json' })
    if ($replays.Count -gt 0 -and $stored.Count -gt 0 -and $packed.Count -eq 0) {
        throw "The export did not pack any res://replays/*.json (check include_filter of the `"$Preset`" preset in visualization\export_presets.cfg)."
    }

    if (-not $Web) {
        Write-Step "4/$steps Adding the replays folder (drop-in replays, no rebuild) and the safe-mode launcher"
        $dropDir = Join-Path $DemoDir "replays"
        New-Item -ItemType Directory -Force -Path $dropDir | Out-Null
        [IO.File]::WriteAllText((Join-Path $dropDir "README.md"), $ReplaysReadme.Replace("`r`n", "`n").Replace("`n", "`r`n"), (New-Object Text.UTF8Encoding($false)))
        Write-Host "   $dropDir"
        $safeModePath = Join-Path $DemoDir "Start in safe mode.bat"
        [IO.File]::WriteAllText($safeModePath, $SafeModeBat.Replace("`r`n", "`n").Replace("`n", "`r`n") + "`r`n", (New-Object Text.ASCIIEncoding))
        Write-Host "   $safeModePath"
    }

    # The key's value reaches the checker through the environment only (never on a command line)
    Write-Step "$(if ($Web) { 4 } else { 5 })/$steps Checking the build: no STM API key, nothing that looks like a secret"
    if ($python) {
        $code = Invoke-Logged $python[0] (@($python | Select-Object -Skip 1) + @("-I", $Checker, $OutDir, "--project=$Project"))
        if ($code -ne 0) { throw "The build has something that must not be shared (see above): $(if ($Web) { 'do not publish it' } else { 'it was not zipped' })." }
    } else {
        Write-Host "   Skipped: no Python." -ForegroundColor Yellow
    }

    if ($Web) {
        $elapsed = (Get-Date) - $started
        # A command to paste in cmd or PowerShell, from the repository root (the full .venv path has spaces)
        $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
        $serve = if (-not $python) { "python" } elseif ($python[0] -eq $venvPython) { ".venv\Scripts\python" } else { $python -join " " }
        Write-Host ""
        Write-Host ("Done in {0:N0} min {1:N0} s." -f [math]::Floor($elapsed.TotalMinutes), $elapsed.Seconds) -ForegroundColor Green
        Write-Host ("   Web build  : {0} ({1:N0} MB)" -f $WebDir, ((Get-ChildItem -LiteralPath $WebDir -File | Measure-Object Length -Sum).Sum / 1MB))
        Write-Host "   Try it     : from the repository root, $serve -m http.server 8060 --bind 127.0.0.1 --directory visualization\Build\Web"
        Write-Host "                then open http://127.0.0.1:8060 (the page does not start when opened from the disk)."
        Write-Host "   Publish    : GitHub Pages, with the `"Web version`" workflow (see the README, `"Web version`")."
        Write-Log @("", "OK: $WebDir")
    } else {
        Write-Step "6/$steps Zipping"
        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::Open($ZipPath, [IO.Compression.ZipArchiveMode]::Create)
        $zipped = $false
        try {
            $prefixLength = $DemoDir.TrimEnd("\").Length + 1
            foreach ($file in Get-ChildItem -LiteralPath $DemoDir -Recurse -File) {
                # Entry names with "/" (CreateFromDirectory writes "\" on Windows PowerShell 5.1)
                $entryName = $file.FullName.Substring($prefixLength).Replace("\", "/")
                [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $entryName, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
            }
            $zipped = $true
        } finally {
            $archive.Dispose()
            # Never leave a truncated zip that looks shareable
            if (-not $zipped) { Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue }
        }

        $elapsed = (Get-Date) - $started
        $zipSize = (Get-Item -LiteralPath $ZipPath).Length / 1MB
        Write-Host ""
        Write-Host ("Done in {0:N0} min {1:N0} s." -f [math]::Floor($elapsed.TotalMinutes), $elapsed.Seconds) -ForegroundColor Green
        Write-Host "   Try it     : $ExePath"
        Write-Host ("   Share this : {0} ({1:N0} MB)" -f $ZipPath, $zipSize)
        $bundled = if ($stored.Count -gt 0) { $packed.Count } else { $replays.Count }
        Write-Host "   Bundled    : $bundled replay(s) from visualization\replays"
        Write-Host "   Unzip, double-click `"STM DT Demo.exe`". More replays: copy *.json files into its replays\ folder."
        Write-Host "   On a computer where it does not start or is too slow: `"Start in safe mode.bat`"."
        Write-Log @("", "OK: $ZipPath")
    }
} catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "BUILD FAILED: $($_.Exception.Message)" -ForegroundColor Red
    try { Write-Log @("", "FAILED: $($_.Exception.Message)") } catch { }
} finally {
    if (-not $NoPause -and (Test-StartedFromExplorer)) {
        Write-Host ""
        Read-Host "Press Enter to close" | Out-Null
    }
}
exit $exitCode
