# Artemis — Local Voice AI Assistant

A desktop assistant that controls your computer by voice, running on a **hybrid
LLM** brain: a free OpenRouter cloud model when online, local Ollama when not
(`core/llm_router.py` decides, transparently to the rest of the app). The model
never touches the OS directly — it can only emit a JSON tool-call; a Python
dispatcher validates and executes the real action. This keeps the assistant
auditable and lets dangerous operations (delete, shutdown, ...) require explicit
confirmation before anything runs.

## Highlights

- **Plugin architecture**: every capability (filesystem, web, Windows control, ...)
  is a self-contained module registered via `@register_tool` — adding a 101st tool
  never touches the dispatcher or the core loop.
- **Safety by construction**: each tool declares a `DangerLevel`; the dispatcher is
  the single choke point that enforces confirmation for anything destructive (see
  `plugins/filesystem_plugin.py::_safe_join` for an example of the path-traversal
  guarding this buys).
- **Local speech recognition** via `faster-whisper`, no cloud STT dependency.
- **Multi-step planning**: `core/planner.py` sequences multi-step commands and
  halts the plan if a step fails, instead of ploughing ahead.
- **Persistent chat & settings GUI** (`ui/chat_window.py`, `ui/settings_window.py`)
  alongside the terminal `--chat`/`--voice` modes.
- **Real DOM-level browser automation**: an own MCP server
  (`mcp_servers/browser_automation_server.py`) drives headless Chromium through
  Playwright — no third-party npm package, and its *test suite* needs no
  network. The tool itself navigates to whatever `url` it is given, so it does
  go out to the internet; by default it is restricted to `http(s)://` URLs on
  public hosts (see below).
- **705 automated tests** exercising real behavior (dispatcher, planner, rate
  limiting, filesystem safety, OpenRouter client, UI) — not mocks.

## Install & run

```bash
pip install -r requirements.txt
ollama pull llama3.1          # or whichever model config.yaml points to
python main.py --chat         # text mode
python main.py --voice        # voice mode (faster-whisper)
```

`pip install` only fetches the *Python package*; Chromium itself is a separate
download and is needed **only** for the browser automation server:

```bash
python -m playwright install chromium
```

To enable it, uncomment the `browser` entry under `mcp_servers:` in
`config/config.yaml` (it ships commented out, so the default install performs
zero I/O).

**Before you enable it — what `run_browser_task` can actually do.** The entry
sets `trusted: true`, which registers the tool as `DangerLevel.SAFE`: it runs
**without asking you for confirmation**. Being Artemis' own code rather than a
third-party package is the right reason for that flag, but "no confirmation"
is not the same as "harmless" — the tool navigates to the `url` it is given and
can type into and click things on the page it finds. To keep that bounded, the
server validates the URL *before* launching a browser and by default allows
only `http://` and `https://` to **public** hosts: `file://` (which could read
your SSH keys straight off disk), `data:`, `javascript:`, and loopback /
private / link-local addresses such as `127.0.0.1`, `192.168.x.x` and
`169.254.169.254` are rejected. To automate your own local service, set
`env: {"ARTEMIS_BROWSER_ALLOW_LOCAL": "1"}` in that entry — that reopens only
the host check, never the `http(s)`-only scheme rule.

Run the test suite with:

```bash
pytest
```

## Project layout

```
core/            dispatcher, planner, LLM client, plugin loader, manifest
config/          Settings (pydantic) + config.yaml
plugins/         one file per capability (filesystem, web, windows, ...)
mcp_servers/     own MCP servers, e.g. Playwright browser automation
memory/          SQLite-backed conversation memory
tests/           ~710 tests covering the above
```

## Architecture deep-dive

The full design log — every architectural decision from v0.1 through the current
version, including two real bugs found and fixed during a security hardening pass
— lives in [`ARCHITECTURE.md`](./ARCHITECTURE.md).
