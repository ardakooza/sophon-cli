# Sophon

Sophon is an open source terminal interface for AI assisted development. The project is an early foundation: it currently includes a terminal UI, provider setup for OpenRouter and Cloudflare Workers AI, configuration display, and safe source checkout updates.

## Install

Requires Python 3.10 or newer, `curl`, and `tar`. The installer downloads the latest source from GitHub and installs Sophon into an isolated user virtual environment. It does not require Git or administrator access.

```sh
curl -fsSL https://sophon-cli.vercel.app/install.sh | sh
```

On Termux, install Python and curl first:

```sh
pkg install python curl
curl -fsSL https://sophon-cli.vercel.app/install.sh | sh
```

Run `sophon` to open the terminal interface. To upgrade, run the install command again. On Linux and macOS, the command is installed in `~/.local/bin`; on Termux it is installed in `$PREFIX/bin`. Add `~/.local/bin` to your `PATH` if your shell does not already include it.

The installer will not overwrite an unrelated existing `sophon` command. Remove or rename that command before installing if needed.

## Configure a provider

Run `sophon setup` to choose OpenRouter or Cloudflare Workers AI and save the connection settings. Run `sophon config` to see the saved provider and model. Settings default to `~/.sophon/config/config.json` (or `$XDG_CONFIG_HOME/sophon/config/config.json` when set); an existing config is moved to the new path on first read. API credentials are stored with restricted file permissions; they are not encrypted.

## Commands

- `sophon` or `sophon run` — open the terminal interface
- `sophon setup` — configure a provider
- `sophon config` — show saved provider settings
- `sophon agent` — start an interactive AI chat using the configured provider
- `sophon agent -p "message"` — send one message and print the response
- `sophon init` — create a starter project configuration
- `sophon update` — safely update a source checkout or refresh an installed package
- `sophon --help` — show command help

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
sophon
```

`src/sophon/VERSION` is the user-facing CLI version in `DD.MM.YYYY – #NNNN` format, where `NNNN` is a four-digit release identifier. `src/sophon/PYPI_VERSION` stores the corresponding PEP 440 package version used by Python installers.

## Website

The English landing page and hosted installer are in `public/`. The project is configured as a static Vercel site with `vercel.json`.

## License

MIT
