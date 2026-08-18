Sellee is a marketplace agent. It helps you list items on peer-to-peer marketplaces, negotiate with buyers, and close sales. It runs locally on your machine.

- **Supported marketplaces**: Carousell
- **Interact with Sellee using**: Telegram, Claude Code

## Features

- **Lists from photos.** Send Sellee photos and a description over Telegram, and it drafts and publishes the listing for you.
- **Negotiates with buyers.** When buyers chat on your listings, Sellee replies, negotiates, and works autonomously to close deals.
- **Escalates what matters.** Buyer questions that need your call reach you on Telegram; everything else it handles on its own.
- **Works in a visible browser.** Sellee interacts with marketplaces through its own dedicated browser, and you can watch it work.
- **Configured conversationally.** Ask Sellee what settings are available. You may want to set the _persona_ setting to control how Sellee speaks to buyers.

## Quick start

This section assumes a native installation on Linux or MacOS. For installation guides for Windows or using Docker, see [docs/install.md](docs/install.md).

Ensure pre-requisites:

- **Browser:** Chrome or Chromium
- **Node runtime and package manager:** Node.js and NPM
- **Agent harness:** Claude Code

Clone the repository and run the setup script:

```sh
git clone https://github.com/carousell/sellee && cd sellee && ./setup
```

Follow the installation instructions. The setup will ask for consent before making changes to your system. You will be guided to log into your marketplaces on your browser, and to optionally link channels (i.e. Telegram) with the agent.

Once you have Sellee set up, try listing an item: send Sellee some photos on Telegram, give it a description, and it will take it from there.

Useful commands:

```sh
sellee chat        # Launch a Claude Code session to chat with your agent
sellee logs --web  # See what the agent is doing
```

Maintenance commands:

```sh
sellee healthcheck
sellee update
sellee uninstall --preserve-data   # --preserve-data will uninstall Sellee but keep configuration and other data
```

## How it works

You interact with Sellee through **control surfaces** — chat apps like Telegram, and agent harnesses like Claude Code. Behind those sits the **Sellee daemon**, a single local process holding all of the core logic: a SQLite store, an event bus for scheduling, and an MCP server that everything the agent does goes through. The **agent harness and browser** are the only components outside the daemon; the harness drives the browser with Playwright, and the browser is where you are signed into your **marketplaces** and where buyers interact with your listings.

For the full details, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

