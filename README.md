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

Run `sophon setup` to choose OpenRouter or Cloudflare Workers AI and save the connection settings. Run `sophon config` to see the saved provider and model; the API key is shown as `hidden`, and the endpoint is not displayed. Settings default to `~/.sophon/config/config.json` (or `$XDG_CONFIG_HOME/sophon/config/config.json` when set); an existing config is moved to the new path on first read. API credentials are stored with restricted file permissions; they are not encrypted.

## Commands

- `sophon` or `sophon run` — open the terminal interface
- `sophon setup` — configure a provider
- `sophon config` — show saved provider settings
- `sophon doctor` — check runtime, provider configuration, and local storage health
- `sophon agent` — start an interactive AI chat using the configured provider
- `sophon agent -m "message"` — send one message using `AGENT.md` instructions
- `sophon agent -p "system prompt" -m "message"` — send a message with custom system guidance
- `sophon agent -t "task"` — run a one-shot task (same behavior as `-m`)
- `sophon init` — create a starter project configuration
- `sophon update` — safely update a source checkout or refresh an installed package
- `sophon --help` — show command help

In the terminal interface, `/restart` restarts Sophon and `/auto-update on|off` controls startup updates (off by default). `/reconfig` starts provider reconfiguration directly. `/backup` saves a private copy of all config data to `~/.sophon/backup/config_backup.json`; `/restore` overwrites the active config only after you type `RESTORE`. `/identity` edits `AGENT.md` directly in a card; use the arrow keys to move the caret and lines, Ctrl+N/Ctrl+K to insert/delete a line, and Enter to save. When auto-update is enabled, Sophon first shows the welcome screen, then animates the update check; it only downloads and restarts when a newer release is found. A successful manual update shows a short restart countdown. Sophon records the detected operating system in the `Environment` field of `~/.sophon/config/config.json`.

Agent mode starts every new session in `chat` mode. `/mode plan` performs read-only inspection and proposes a plan; approve implementation with `/mode build`. `/mode build` implements requested changes, while `/mode chat` is for general discussion. The current mode and reasoning state are shown in the footer. Press Escape twice while a response is in progress to stop it; the first press shows a cancellation hint. Slash-command names appear as light inline completions while typing and Enter accepts a completion.

In Agent mode, `/cwd` shows the current directory and `/cd` opens a tree picker rooted at the current directory, showing up to five levels; selected directories become the active project. `/sessions` lists saved sessions alphabetically with context usage and dates, and its picker opens the selected session. `/fork` creates a separate session from a selected session, and `/delete` removes a selected session after confirmation. `/history` browses this session's tool calls, redacted arguments, outcomes, timing, and file-change size/line summaries without storing file contents. `/review` runs a static review of the latest Agent edits; `/undo` safely reverts the latest Agent file operations when they have not been changed since. `/usage` shows session token/request totals and provider limits. `/clear` clears the screen without starting a new session; `/new` starts a new session. `/changelog` shows the release date and recent changes. `/reasoning on|off` toggles reasoning when the selected model supports it; `/reasoning-effort low|medium|high` persists the requested effort. Long footers move Context to a second line rather than clipping the status.

Context usage appears in the Agent footer. `/compact` becomes available at 50% context usage. Automatic compression is on by default at 80%; use `/auto-compression on|off` to toggle it and `/threshold 30–95` to set its percentage threshold. These preferences are stored in `~/.sophon/config/config.json` and intentionally omitted from the config card. Sophon keeps recent turns and replaces earlier history with a concise summary.

The Agent has built-in `get_version`, `is_update`, `update`, `system_health`, `workspace_list`, `session_list`, `session_rename`, `get_time`, and `get_date` tools. `sophon doctor` also checks the Agent tool registry and, when configured, tests the provider's model catalog endpoint without making a billable inference request.

Agent project files live in `~/.sophon/workspace/my-project` by default. Each project has its own folder under `~/.sophon/workspace/`; the root `WORKSPACE.md` describes how to organize that workspace. Every Agent session receives the editable instructions from `~/.sophon/agent/AGENT.md`; its path is recorded in `~/.sophon/config/config.json`.

Plan and build modes can call `ask` to show a sequential Questions card with one to six questions, up to three preset answers per question, custom input, and per-question skip; the final Enter submits the responses. For multi-step tasks, `planning` maintains `TODO.md` in the active project and marks numbered steps complete. Its checklist is an Agent-managed project file.

File operations are grouped under the `File Manage` tool category: create/edit/read/delete/rename/copy/move files; create/rename/copy/move folders; view a five-level tree; and create/extract ZIP archives. All paths are resolved and checked against the active project and `allowed_path` before access. The active project is always allowed. `allowed_path` is a JSON array in `~/.sophon/config/config.json`; each absolute path or project-relative path explicitly grants that path and its descendants. Symlink paths are rejected, destinations are not overwritten, tree/archive operations are bounded, and ZIP extraction rejects traversal and symbolic links. For example: `"allowed_path": ["~/Documents/project-data"]`. The config has `mode.plan`, `mode.build`, and `mode.chat` tool-name lists. Empty lists retain the built-in defaults; a non-empty list restricts both the tools advertised to the model and the tools accepted at execution time.

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
