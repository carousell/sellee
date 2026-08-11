# Installation

- [Docker (all platforms)](#docker-all-platforms)
- [Linux (native)](#linux-native)
- [MacOS (native)](#macos-native)
- [Windows (native)](#windows-native)

## Docker (all platforms)

> [!NOTE]  
> These instructions will also apply to Podman or other container engines that support Docker compose files.

Clone the repository:

```sh
git clone https://github.com/carousell/selly-agent
cd selly-agent
```

Add a `.env` file containing the `TZ` and `CLAUDE_CODE_OAUTH_TOKEN` env vars. You can get your Claude Code token using `claude setup-token`.

```env
TZ=Asia/Singapore
CLAUDE_CODE_OAUTH_TOKEN=...
```

Since Selly will be running in a container, it cannot manage its own Chrome process. You will have to launch Chrome separately. A `start-chrome` script is included in the repository. Execute using:

- **Windows**: `powershell -ExecutionPolicy Bypass -File .\start-chrome.ps1`
- **Linux/MacOS**: `./start-chrome.sh`

Build and run the container. Omit the `-d` flag if you prefer to run Selly in the foreground.

```
docker compose build
docker compose up -d
```

Complete the setup:

```
docker exec -it selly-agent selly-agent setup
```

From this point, you can follow the rest of the quick start guide in the [README.md](/README.md#quick-start). Note instead of `selly-agent`, you will need to use `docker exec -it selly-agent selly-agent`. Writing a wrapper script for convenience is recommended.

To update Selly, pull the latest revision, rebuild the image and restart the container:

```
git pull && docker compose build && docker compose up -d
```

Supported environment variables:

| variable | required | what it does |
| --- | --- | --- |
| `TZ` | yes | the container's timezone; must match where you sell |
| `CLAUDE_CODE_OAUTH_TOKEN` | yes | how the `claude` CLI authenticates in here |
| `SELLY_DATA` | no | host directory to mount at `/data` (default `./selly-data`) |
| `SELLY_CDP_PORT` | no | the CDP port, both sides of the forwarder (default `9222`) |
| `SELLY_CDP_HOST` | no | what the forwarder points at (default `host.docker.internal`) |
| `SELLY_CDP_FORWARD` | no | `0` turns the forwarder off; the Linux override sets it |
| `SELLY_CHROME_BIN` | no | read by the launch scripts when Chrome is somewhere unusual |
| `SELLY_CHROME_PROFILE` | no | where the launch scripts keep the agent's Chrome profile |
| `SELLY_BIND_HOST` | no | what the daemon's HTTP server binds (image sets `0.0.0.0`) |

## Linux (native)

> [!NOTE]  
> Implemented but not yet verified on a real Linux desktop. Until it has been, prefer the Docker instructions above.

Requires a glibc distribution with systemd — Ubuntu, Debian, Fedora, Mint and their derivatives. Alpine and other musl distributions are not supported (the pinned Python has no musl build); neither is a WSL distribution started without systemd, though enabling it works:

```ini
# /etc/wsl.conf
[boot]
systemd=true
```

The background worker runs as a **systemd user unit** in your desktop login session — it starts when you log in and stops when you log out. It is deliberately not set to linger past logout, because the Chrome it drives lives in that session anyway.

Prerequisites, if setup reports them missing:

```sh
sudo apt install nodejs npm      # Debian, Ubuntu, Mint
sudo dnf install nodejs npm      # Fedora, RHEL
```

Take Google Chrome from [google.com/chrome](https://www.google.com/chrome) as a `.deb` or `.rpm`. Snap and flatpak Chromium builds are untested: they confine filesystem access, and the agent keeps a Chrome profile of its own under your home directory.

Then:

```sh
./setup
```

Useful afterwards:

```sh
systemctl --user status selly-agent      # what systemd thinks of the worker
journalctl --user -u selly-agent         # its startup failures
```

The worker's own output goes to `~/.local/state/selly-agent/logs/`, which is what `selly-agent logs` reads.

## MacOS (native)

```sh
./setup
```

## Windows (native)

Coming soon.
