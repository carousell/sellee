# The installer's front door on Windows — the sibling of ./setup, and deliberately almost nothing.
#
# It does the one job Python cannot do for itself: establish the interpreter to hand over to. Not
# whatever python.exe happens to be on PATH — which on Windows is as likely to be the Microsoft
# Store stub that opens a shop page as a real interpreter — but a standalone one uv provisions at
# the version .python-version pins, followed by this release's dependencies from uv.lock. Every
# decision, prompt and write after that lives in the Python installer, shared with `selly-agent
# update` and with the POSIX front door.
#
# The pin file is the same one ./setup and installer/runtime.py read, so the uv version and its
# per-platform digests are recorded once even though this fetch logic is a third copy of them.
#
# Windows PowerShell 5.1 is the floor: it is what ships with Windows, and requiring PowerShell 7
# would mean installing something before the thing that installs things.

[CmdletBinding()]
param(
    # Stop once the runtime is ready, installing nothing on the machine, and also install the dev
    # dependency group. Together these let a checkout be prepared without being installed.
    [switch]$BootstrapOnly,
    [switch]$WithDev,
    # Everything else is forwarded to the Python installer untouched.
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Forwarded
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# The output is a transcript someone reads while deciding whether to continue, so it has to be
# legible: UTF-8 for the box-drawing and the ticks. Per-process, so nothing about the machine's
# console is changed permanently. (Colour is the installer's own business — it only emits VT
# sequences where the host is known to render them.)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Die([string]$Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pinFile = Join-Path $here 'src\selly_agent\data\uv-pin.txt'
if (-not (Test-Path -LiteralPath $pinFile)) {
    Die "this tree is missing $pinFile; re-download the release."
}

# --- the pin ---------------------------------------------------------------------------------

$uvVersion = ''
$digests = @{}
foreach ($line in Get-Content -LiteralPath $pinFile) {
    $fields = -split $line.Trim()
    if ($fields.Count -eq 2 -and $fields[0] -eq 'version') { $uvVersion = $fields[1] }
    elseif ($fields.Count -eq 3 -and $fields[0] -eq 'sha256') { $digests[$fields[1]] = $fields[2] }
}
if (-not $uvVersion) { Die "$pinFile names no uv version." }

# uv's release archives are named by Rust target triple. Kept in step with the table in
# selly_agent/installer/runtime.py and the one in ./setup.
$triple = switch ($env:PROCESSOR_ARCHITECTURE) {
    'ARM64' { 'aarch64-pc-windows-msvc' }
    'AMD64' { 'x86_64-pc-windows-msvc' }
    default { Die "no pinned uv build for $($env:PROCESSOR_ARCHITECTURE)." }
}
$expected = $digests[$triple]
if (-not $expected) {
    Die "$pinFile records no digest for $triple; refusing to run an unverified binary."
}

$pythonVersionFile = Join-Path $here '.python-version'
if (-not (Test-Path -LiteralPath $pythonVersionFile)) {
    Die "this tree is missing .python-version; re-download the release."
}
$pinnedPython = (Get-Content -LiteralPath $pythonVersionFile -TotalCount 1).Trim()

# --- acquiring uv ----------------------------------------------------------------------------

# Deliberately not a directory on PATH: the user may have their own uv, and taking that name is
# not ours to do. Mirrors paths.uv_path().
# Mirrors paths.tools_dir(): the override wins, and without one the Windows layout puts the four
# roots under a single %LOCALAPPDATA% tree. A different answer here would leave `update` fetching a
# second uv and `uninstall` unable to find the first.
$dataRoot = if ($env:XDG_DATA_HOME) {
    Join-Path $env:XDG_DATA_HOME 'selly-agent'
}
else {
    Join-Path $env:LOCALAPPDATA 'selly-agent\share'
}
$tools = Join-Path $dataRoot 'tools'
$ourUv = Join-Path $tools 'uv.exe'

function ServesPin([string]$Candidate) {
    # Whether a uv can offer a *final* build of the interpreter we pin — asked, not inferred from
    # uv's own version, because uv refreshes its list of downloadable interpreters over the network.
    # One that cannot reach that list falls back to what was baked in when it was built, which for
    # an old enough uv is a pre-release.
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) { return $false }
    try { $listed = & $Candidate python list 2>$null } catch { return $false }
    $escaped = [regex]::Escape($pinnedPython)
    return [bool]($listed | Select-String -Pattern "^cpython-$escaped(\.\d+)*-" -Quiet)
}

$onPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (ServesPin $onPath) {
    $uv = $onPath
    Write-Output "using the uv already on your PATH ($(& $uv --version))"
}
elseif (ServesPin $ourUv) {
    $uv = $ourUv
    Write-Output "using the uv from a previous install ($(& $uv --version))"
}
else {
    Write-Output "Fetching uv $uvVersion - it provisions the Python this runs on."
    $asset = "uv-$triple.zip"
    $url = "https://github.com/astral-sh/uv/releases/download/$uvVersion/$asset"
    $work = Join-Path ([System.IO.Path]::GetTempPath()) "selly-uv-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    try {
        $archive = Join-Path $work $asset
        try {
            # Invoke-WebRequest rather than curl: it is present on every supported Windows, where
            # curl.exe only arrived in 1803 and is still absent from some images.
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
        }
        catch { Die "couldn't download $url" }

        $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected.ToLower()) {
            Die "$asset does not match the digest recorded for uv $uvVersion - refusing it."
        }

        Expand-Archive -LiteralPath $archive -DestinationPath $work -Force
        $extracted = Get-ChildItem -Path $work -Filter 'uv.exe' -Recurse | Select-Object -First 1
        if (-not $extracted) { Die "the uv archive doesn't contain uv.exe." }
        New-Item -ItemType Directory -Path $tools -Force | Out-Null
        # Move into place, so a half-written binary is never runnable at the final name.
        Move-Item -LiteralPath $extracted.FullName -Destination $ourUv -Force
    }
    finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    $uv = $ourUv
    Write-Output "uv $uvVersion installed at $uv"
}

# --- the runtime -----------------------------------------------------------------------------

Write-Output 'Preparing the Python runtime and dependencies...'
& $uv python install --project $here | Out-Null
if ($LASTEXITCODE -ne 0) { Die "uv couldn't install the Python version this release pins." }

if ($WithDev) {
    & $uv sync --locked --project $here
}
else {
    & $uv sync --locked --no-dev --project $here | Out-Null
}
if ($LASTEXITCODE -ne 0) { Die "uv couldn't install this release's dependencies." }

$python = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Die "the dependency install left no interpreter at $python."
}

if ($BootstrapOnly) {
    Write-Output "runtime ready: $(& $python -V)"
    exit 0
}

# No exec on Windows: the installer is run as a child and its exit status becomes ours. Anything
# else would return this shell to a prompt while the install was still going.
& $python (Join-Path $here 'bin\selly-agent') setup @Forwarded
exit $LASTEXITCODE
