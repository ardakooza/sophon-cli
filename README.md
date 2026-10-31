# Xernel

Xernel is an open source terminal interface for AI assisted development. The project is an early foundation: it currently includes a terminal UI, provider setup for OpenRouter and Cloudflare Workers AI, configuration display, and safe source checkout updates.

## Install

Requires Python 3.10 or newer, `curl`, and `tar`. The installer downloads the latest source from GitHub and installs Xernel into an isolated user virtual environment. It does not require Git or administrator access.

```sh
curl -fsSL https://xernel.vercel.app/install.sh | sh
```

On Termux, install Python and curl first:

```sh
pkg install python curl
curl -fsSL https://xernel.vercel.app/install.sh | sh
```

Run `xernel` to open the terminal interface. To upgrade, run the install command again. On Linux and macOS, the command is installed in `~/.local/bin`; on Termux it is installed in `$PREFIX/bin`. Add `~/.local/bin` to your `PATH` if your shell does not already include it.

The installer will not overwrite an unrelated existing `xernel` command. Remove or rename that command before installing if needed.

## Configure a provider

Run `xernel setup` to choose OpenRouter or Cloudflare Workers AI and save the connection settings. Run `xernel config` to see the saved provider and model. API credentials are stored in the user configuration file with restricted file permissions; they are not encrypted.

## Commands

- `xernel` or `xernel run` — open the terminal interface
- `xernel setup` — configure a provider
- `xernel config` — show saved provider settings
- `xernel init` — create a starter project configuration
- `xernel update` — safely update a source checkout or refresh an installed package
- `xernel --help` — show command help

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
xernel
```

`src/xernel/VERSION` is the user-facing CLI version in `DD.MM.YYYY - NN` format. Increment `NN` for each release that day, starting at `00`; begin a new date at `00`. `src/xernel/PYPI_VERSION` stores the corresponding PEP 440 package version used by Python installers.

## Website

The English landing page and hosted installer are in `public/`. The project is configured as a static Vercel site with `vercel.json`.

## License

MIT
