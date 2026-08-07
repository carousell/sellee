# Running selly-agent in a container

The second install profile. The host install (`./setup`) is still the primary
one; this exists for two cases:

- **You would rather nothing were installed on your machine.** Everything lands
  in one directory and inside an image. Uninstalling is `docker compose down`
  and deleting that directory.
- **You are on Windows.** The native Windows port exists but has not been
  verified on real hardware yet, so the container — running the Linux daemon we
  already trust — is the shorter path.

It is deliberately **not** a server deployment. Chrome runs on your own desktop,
because the whole premise of the agent is that it drives your real, logged-in
browser rather than a fresh headless one that every marketplace's anti-bot
system can see coming. Nothing here runs while your machine is off.

Every command below is Docker Compose, because that is the recipe we ship. The
program itself never names an engine: it knows only that it is in a container,
so its own messages say "in the container" and leave the rest to you. Podman,
`docker run` by hand, or anything else that can run the image and mount `/data`
will do — translate the commands as you go.

## The shape of it

```
        your computer                            the container
────────────────────────────────────────────────────────────────────────────
 Chrome (you start it,                    socat 127.0.0.1:9222
   CDP on 127.0.0.1:9222)  ◄────────────►   └► host.docker.internal:9222
   its own profile                                (Linux: network_mode host,
                                                   no forwarder)
                                           daemon ─ MCP + control on the
 <your data dir>/  ── bind mount ──► /data   │     container's loopback,
   inbox/     (drop photos here)             │     not published
   share/ state/ config/ cache/              ├─ claude -p   (headless passes)
                                             └─ npx @playwright/mcp ──► CDP

 docker exec -it selly-agent selly-agent chat ──► claude (attended session)
```

## Getting started

```sh
git clone https://github.com/carousell/selly-agent && cd selly-agent

export TZ=Asia/Singapore                        # your own timezone
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"

docker compose build
./start-chrome.sh                               # start-chrome.ps1 on Windows
docker compose up -d
docker exec -it selly-agent selly-agent setup
```

`setup` in a container runs only the half that is about you — where you sell,
the carousell.ai key, marketplace sign-in, Telegram, and the terminal session's
workspace. The machine half is the image.

Then:

```sh
docker exec -it selly-agent selly-agent chat            # talk to it
docker exec -it selly-agent selly-agent healthcheck     # is anything wrong
docker exec -it selly-agent selly-agent logs --follow   # watch it work
docker compose logs -f selly-agent                      # the worker's own stderr
```

## The two settings you have to get right

**`TZ`.** Every timing decision the agent makes is made against its own clock:
quiet hours for publishing, and quiet hours for messaging you. A container's
clock defaults to UTC, so left unset, a 22:00–08:00 quiet window would be
applied to the wrong eight hours and the agent would list things at four in the
morning. The container refuses to start without it, `healthcheck` has a line for
it, and a mismatch also arrives as a notice.

**`CLAUDE_CODE_OAUTH_TOKEN`.** The `claude` CLI runs inside the container, and a
container has no browser to complete a sign-in with. Mint a token on your own
machine (`claude setup-token`) and set it before `docker compose up`. The
container refuses to start without one; `healthcheck`'s harness line reports one
that has expired.

Neither is needed to `docker compose build` — the image holds no secrets.

Both can live in a `.env` file beside `compose.yaml` instead of your shell —
compose reads it automatically, and it is excluded from git and from the build
context so the token never reaches an image layer:

```
TZ=Asia/Singapore
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-EXAMPLE-NOT-A-REAL-TOKEN
```

No quotes and no spaces around the `=`. To keep it out of the checkout entirely,
put it anywhere you like and pass `--env-file C:\path\to\selly.env` to
`docker compose`.

Either way the token ends up in the container's environment, where
`docker inspect` can read it. That is inherent to passing a secret this way; if
that matters for your machine, the alternative is a secrets manager rather than
a different file.

## Chrome

You start it, with `./start-chrome.sh` (`start-chrome.ps1` on Windows). It opens
a Chrome with a profile of its own — not your everyday one — so your tabs and
logins are untouched, and the marketplace sessions the agent uses live in that
separate profile. Sign in to your marketplaces there, or let
`docker exec -it selly-agent selly-agent connect carousell` open the login page
for you in that same window.

The scripts pass exactly the flags a host install would, and keep the debugging
port on loopback: it is browser control with no authentication, and it should
not be reachable from your network. The container gets to it through a
forwarder, not by the port being exposed.

Closing Chrome is not a failure. The agent notices, says so once, and waits;
start it again and it picks up. Nothing recovers it for you, though — so after
a reboot, run the script again (a login item is a reasonable thing to add
yourself; the install will not create one).

If your machine is not always on, the honest expectation is that the agent works
when it is.

## Photos

Two ways in, and the phone one is easier:

- **Telegram.** Send the photo to the bot. The poller downloads it into the
  media store before any model sees it, and no file ever touches your
  filesystem. Nothing about the container is involved.
- **The inbox directory.** Drop files into `<your data dir>/inbox/` on your
  machine; inside the container that is `/data/inbox/`, and that is the path you
  name in the attended session:

  ```
  import the photos in /data/inbox/lamp-1.jpg and /data/inbox/lamp-2.jpg
  ```

  It is a convention, not a rule — any path inside `/data` works. It exists
  because the attended session runs inside the container, so both ends have to
  agree on what a path means.

One real limit: a single upload of more than ~50 MB of photos is refused by
Playwright, which ships the bytes to the browser rather than pointing at them.
That is true of a host install too. Ten normal phone photos are nowhere near it.

## On a Linux host

Docker Desktop (macOS, Windows) runs the container in a VM and routes
`host.docker.internal` to your machine, including services on its loopback — so
the forwarder inside the container works. A Linux host has no such route: the
bridge address cannot reach a service bound to `127.0.0.1`, which the agent's
Chrome deliberately is. Use the override, which shares the host's network
namespace instead and runs no forwarder:

```sh
docker compose -f compose.yaml -f compose.linux.yaml up -d
```

One wrinkle there: the container runs as root, so files it creates in the bind
mount belong to root on your machine (`sudo rm -rf` to remove them). The photo
inbox is created world-writable for exactly this reason. On macOS and Windows,
Docker Desktop maps ownership for you and none of this applies.

Podman is likely to work — set `SELLY_CDP_HOST=host.containers.internal`, its
own alias for the host — but is not a verified target.

## Updating and removing

A new version is a new image:

```sh
git pull && docker compose build && docker compose up -d
```

`selly-agent update`, `selly-agent uninstall` and `selly-agent daemon
install|start|stop` all refuse inside the container: they manage a host install's
version directory and login job, neither of which exists here. Your data is in
the bind mount, so a rebuild keeps everything — listings, threads, settings,
marketplace sessions.

Removing it:

```sh
docker compose down
rm -rf selly-data      # or whatever you set SELLY_DATA to
```

That is all of it. Nothing was written anywhere else — no launch agent, no
shell rc line, no `~/.local/share`. The one thing left behind is the Chrome
profile the launch script created (`~/.selly-agent/chrome-profile`, or
`%LOCALAPPDATA%\selly-agent\chrome-profile`), which holds your marketplace
sessions; delete it too if you want them gone.

`selly-agent uninstall` refuses inside the container: run there it would empty
your data directory and leave the container running.

## What the container does not do

- **Publish any ports.** The daemon's HTTP server is its MCP and control
  surface, on the container's own loopback. The attended session reaches it from
  inside, which is why it is `docker exec` rather than a `claude` on your
  machine.
- **Supervise anything itself.** Your restart policy is the keep-alive, so
  `daemon install|start|stop` refuse. `daemon status` still works — it reads the
  instance lock rather than asking a supervisor.
- **Run Chrome.** Covered above, and it is the one decision this profile is
  built around.

## Environment reference

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
