# The `irm | iex` bootstrap on Windows — the sibling of install.sh, and just as small.
#
# It can vouch for exactly one property: that the archive it downloaded is the one whose checksum
# was published. Everything after that is the release's own setup.ps1, which is versioned,
# reviewable in the repo, and prints where it will write before it writes anything. Logic inlined
# here instead would be logic served from a URL with no version and no review.
#
# Windows PowerShell 5.1 is the floor — what ships with Windows.

[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Forwarded)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoUrl = 'https://github.com/carousell/selly-agent'
$BaseUrl = $env:SELLY_INSTALL_BASE_URL

function Say([string]$Message) { Write-Output $Message }
# `throw`, never `exit`: the advertised invocation is `irm | iex`, which runs this text inside
# the person's own session — `exit` there closes their window rather than returning to a prompt.
function Die([string]$Message) { throw $Message }

# --- not yet ---------------------------------------------------------------------------------
# Release hosting is not public yet, so the honest answer is that this path does not work rather
# than a 404 halfway through. Setting a base URL is how an end-to-end test exercises the real code
# path. REMOVE THIS BLOCK at cutover, when releases are published.
if (-not $BaseUrl) {
    [Console]::Error.WriteLine('  Clone the repo and run .\setup.ps1 instead:')
    [Console]::Error.WriteLine("    git clone $RepoUrl; cd selly-agent; .\setup.ps1")
    Die "installing with this script isn't supported yet."
}

# --dev points the install at the tree it was run from, and this one is a temp directory deleted
# the moment setup returns — the install would be dead on arrival, with no error.
if ($Forwarded -contains '--dev') {
    Die '--dev needs a checkout; clone the repo and run .\setup.ps1 --dev there.'
}

Say "Here's what this does, before it does any of it:"
Say "  1. Download $BaseUrl/SHA256SUMS"
Say '  2. Download the selly-agent archive it names, and check it against that checksum'
Say '  3. Unpack it into a temporary directory, deleted when this finishes'
Say '  4. Run the unpacked setup.ps1, which fetches the Python it runs on and then lists'
Say '     everywhere it writes before writing'
Say ''

if ($env:OS -ne 'Windows_NT') { Die 'this script is the Windows one; use install.sh elsewhere.' }

# No python check here: the release's own setup.ps1 provisions the interpreter it needs, so the
# machine having one — or having a usable one — is not a precondition for installing.

$work = Join-Path ([System.IO.Path]::GetTempPath()) "selly-install-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $work -Force | Out-Null
try {
    $ProgressPreference = 'SilentlyContinue'

    Say 'Fetching the checksum file...'
    $sumsPath = Join-Path $work 'SHA256SUMS'
    try { Invoke-WebRequest -Uri "$BaseUrl/SHA256SUMS" -OutFile $sumsPath -UseBasicParsing }
    catch { Die "couldn't download $BaseUrl/SHA256SUMS" }

    # The archive's name is read out of the checksum file, so there is no second source to disagree
    # with it, no API call, and nothing to parse JSON with.
    $sums = @{}
    foreach ($line in Get-Content -LiteralPath $sumsPath) {
        $fields = -split $line.Trim()
        if ($fields.Count -ge 2) { $sums[$fields[1].TrimStart('*')] = $fields[0] }
    }
    $archives = @($sums.Keys | Where-Object { $_ -like 'selly-agent-*.tar.gz' })
    if ($archives.Count -eq 0) { Die "SHA256SUMS doesn't name a selly-agent archive." }
    if ($archives.Count -gt 1) {
        # A release directory holds exactly one. More than one means guessing which code to run.
        Die "SHA256SUMS names more than one archive; can't tell which release to install."
    }
    $archive = $archives[0]

    Say "Downloading $archive"
    $archivePath = Join-Path $work $archive
    try { Invoke-WebRequest -Uri "$BaseUrl/$archive" -OutFile $archivePath -UseBasicParsing }
    catch { Die "couldn't download $BaseUrl/$archive" }

    Say 'Checking it against the published checksum...'
    $actual = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $sums[$archive].ToLower()) {
        Die "$archive does not match its published checksum - refusing to run it."
    }

    Say 'Unpacking...'
    # tar.exe has shipped with Windows since 1803 and reads gzip, so the archive format is shared
    # with every other platform rather than being a second thing to publish.
    & tar.exe -xzf $archivePath -C $work
    if ($LASTEXITCODE -ne 0) { Die "couldn't unpack $archive." }
    $tree = Get-ChildItem -Path $work -Directory -Filter 'selly-agent-*' | Select-Object -First 1
    $entry = if ($tree) { Join-Path $tree.FullName 'setup.ps1' } else { $null }
    if (-not $entry -or -not (Test-Path -LiteralPath $entry)) {
        Die "the archive doesn't contain a runnable setup.ps1."
    }

    Say 'Handing over to the installer.'
    Say ''
    # No stdin juggling: `irm | iex` runs this script from a string rather than piping it through
    # stdin, so the console is still attached and setup's prompts reach the person at it.
    & $entry @Forwarded
    $setupExit = $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
# Reported as an error rather than `exit`ed for the same session-survival reason as Die above.
if ($setupExit -ne 0) { Die "setup.ps1 exited with status $setupExit." }
