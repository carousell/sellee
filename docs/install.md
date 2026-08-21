# Installation

- [Docker (all platforms)](#docker-all-platforms)
- [Linux (native)](#linux-native)
- [MacOS (native)](#macos-native)
- [Windows (native)](#windows-native)

Native installations will require the following pre-requisites:

- **Browser:** Chrome or Chromium
- **Node runtime and package manager:** Node.js and NPM
- **Agent harness:** Claude Code

## Docker (all platforms)

> [!NOTE]  
> These instructions will also apply to Podman or other container engines that support Docker compose files.

Clone the repository:

```sh
git clone https://github.com/carousell/sellee
cd sellee
```

Add a `.env` file containing the `TZ` env var and **one** credential for the `claude` CLI:

- `CLAUDE_CODE_OAUTH_TOKEN` if you're using a Claude subscription. Get it with `claude setup-token`.
- `ANTHROPIC_API_KEY` if you're using API key billing.

```env
TZ=Asia/Singapore
CLAUDE_CODE_OAUTH_TOKEN=...
# or, instead of the token:
# ANTHROPIC_API_KEY=...
```

Since Sellee will be running in a container, it cannot manage its own Chrome process. You will have to launch Chrome separately. A `start-chrome` script is included in the repository. Execute using:

- **Windows**: `powershell -ExecutionPolicy Bypass -File .\start-chrome.ps1`
- **Linux/MacOS**: `./start-chrome.sh`

Build and run the container. Omit the `-d` flag if you prefer to run Sellee in the foreground.

```
docker compose build
docker compose up -d
```

Complete the setup:

```
docker exec -it sellee sellee setup
```

From this point, you can follow the rest of the quick start guide in the [README.md](/README.md#quick-start). Note instead of `sellee`, you will need to use `docker exec -it sellee sellee`. Writing a wrapper script for convenience is recommended.

To update Sellee, pull the latest revision, rebuild the image and restart the container:

```
git pull && docker compose build && docker compose up -d
```

Supported environment variables:

| variable | required | what it does |
| --- | --- | --- |
| `TZ` | yes | the container's timezone; must match where you sell |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of these two | how the `claude` CLI authenticates in here; uses your Claude subscription |
| `ANTHROPIC_API_KEY` | one of these two | the API-key alternative; per-token Console billing, and it wins if both are set |
| `SELLEE_DATA` | no | host directory to mount at `/data` (default `./sellee-data`) — holds your tokens and session, so point it **outside** this repository; only the default name is git-ignored |
| `SELLEE_CDP_PORT` | no | the CDP port, both sides of the forwarder (default `9222`). Pinned here because the forwarder must be listening before Chrome starts. If you change it, set `chrome_cdp_port` to match |
| `SELLEE_CDP_HOST` | no | what the forwarder points at (default `host.docker.internal`) |
| `SELLEE_CDP_FORWARD` | no | `0` turns the forwarder off; the Linux override sets it |
| `SELLEE_CHROME_BIN` | no | read by the launch scripts when Chrome is somewhere unusual |
| `SELLEE_CHROME_PROFILE` | no | where the launch scripts keep the agent's Chrome profile |
| `SELLEE_BIND_HOST` | no | what the daemon's HTTP server binds (image sets `0.0.0.0`) |

## Linux (native)

Requires a glibc distribution with systemd. Alpine and other musl distributions are not supported. The daemon runs as a systemd user unit in your desktop login session.

Then:

```sh
./setup
```

Useful afterwards:

```sh
systemctl --user status sellee     # daemon status
journalctl --user -u sellee        # startup failures
```

## MacOS (native)

```sh
./setup
```

## Windows (WSL)

Ensure that systemd is enabled:

```ini
# /etc/wsl.conf
[boot]
systemd=true
```

## Windows (native)

Coming soon.
