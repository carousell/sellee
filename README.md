# Selly

Selly is a marketplace agent. It helps you list items on peer-to-peer marketplaces, negotiate with buyers, and close sales. It runs locally on your machine.

- **Supported marketplaces**: Carousell
- **Interact with Selly using**: Telegram, Claude Code

## Quick start

This section assumes a native installation on Linux or MacOS. For installation guides for Windows or using Docker, see [docs/install.md](docs/install.md).

Ensure pre-requisites:

- **Browser:** Chrome or Chromium
- **Node runtime and package manager:** Node.js and NPM
- **Agent harness:** Claude Code (with Claude subscription)

Clone the repository:

```sh
git clone https://github.com/carousell/selly-agent
cd selly-agent
```

Run the setup script:

```sh
./setup
```

Follow the installation instructions. The setup will ask for consent before making changes to your system. You will be guided to log into your marketplaces on your browser, and to optionally link channels (i.e. Telegram) with the agent.

Useful commands:

```sh
selly-agent chat        # Launch an Claude Code session to chat with your agent
selly-agent logs --web  # See what the agent is doing
```

Once you have Selly set up, try listing an item. If you have Telegram connected, send Selly some photos, give it a description, and Selly will help you list. When buyers chat on your listings, Selly will interact with them. It will escalate buyer questions to you as needed, but otherwise will work autonomously to close deals. Selly interacts with marketplaces using its dedicated browser, and you can watch it work.

Selly settings are configured conversationally. Try asking Selly what settings are available. You may want to set the _persona_ setting to control how Selly speaks to buyers.

Maintenance commands:

```
selly-agent healthcheck
selly-agent update
selly-agent uninstall --preserve-data   # --preserve-data will uninstall Selly but keep configuration and other data
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
