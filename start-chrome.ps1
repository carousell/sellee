# Start the Chrome the agent drives — on this computer, with a profile of its own, so your
# everyday tabs and logins are untouched. Leave it running while the agent is working; close it
# and the agent says so and waits.
#
# The switches are the ones every other platform uses. The debugging port stays on loopback: it
# is browser control with no authentication.
#
# Run it with:  powershell -ExecutionPolicy Bypass -File .\start-chrome.ps1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$port = if ($env:SELLEE_CDP_PORT) { $env:SELLEE_CDP_PORT } else { '9222' }
$profileDir = if ($env:SELLEE_CHROME_PROFILE) {
	$env:SELLEE_CHROME_PROFILE
} else {
	Join-Path $env:LOCALAPPDATA 'sellee\chrome-profile'
}

# Two Chromes on one profile: the second either hangs or opens it read-only.
try {
	Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 `
		-Uri "http://127.0.0.1:$port/json/version" | Out-Null
	Write-Host "The agent's Chrome is already running on port $port - nothing to do."
	exit 0
} catch {
	# Not answering, which is the normal case: carry on and start it.
}

$chrome = $env:SELLEE_CHROME_BIN
if (-not $chrome) {
	$candidates = @(
		"$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
		"${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
		"$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
	)
	$chrome = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $chrome) {
	Write-Error 'Could not find Google Chrome. Install it, or set SELLEE_CHROME_BIN to its path.'
	exit 1
}

New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

Write-Host "Starting the agent's Chrome (profile: $profileDir, debugging port: $port)."
Write-Host 'Leave that window open while the agent is working.'

Start-Process -FilePath $chrome -ArgumentList @(
	"--remote-debugging-port=$port",
	"--user-data-dir=$profileDir",
	'--disable-backgrounding-occluded-windows',
	'--no-first-run',
	'--no-default-browser-check',
	'--restore-last-session',
	'--hide-crash-restore-bubble',
	'--window-position=80,80',
	'--window-size=1200,900'
)

Write-Host 'Started. Sign in to your marketplaces in that window if you have not already.'
