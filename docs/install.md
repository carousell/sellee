# Installation

- [Linux & MacOS](#linux--macos)
    - [Native](#native)
    - [Docker](#docker)
- [Windows](#windows)
    - [Native](#native-1)
    - [Docker](#docker-1)

## Linux & MacOS

> [!WARNING]  
> Linux support is coming soon. However it is expected to be similar to MacOS, requiring no additional work.

### Native

Native installation on Linux and MacOS is well supported

```sh
./setup
```

### Docker

Coming soon.

## Windows

### Native

Coming soon.

### Docker

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

Since Selly will be running in a container, it cannot manage its own Chrome process. You will have to launch Chrome separately. A `start-chrome.ps1` script is included in the repository. Execute using:

```
powershell -ExecutionPolicy Bypass -File .\start-chrome.ps1
```

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
