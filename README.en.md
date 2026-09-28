# Artemis — Local Voice AI Assistant

<!-- TODO: screenshot to be added -->

[🇹🇷 Türkçe](./README.md)

## Description

Artemis is a desktop assistant for Windows that speaks Turkish: you give it voice
commands and it actually operates your computer — opening files, creating folders,
closing apps, searching the web, taking screenshots. The model never touches the OS
directly; it only ever emits a JSON tool-call, which `core/dispatcher.py` validates
and executes. Every tool declares a `DangerLevel`, so destructive operations
(delete, shutdown, lock, ...) require explicit confirmation before anything runs.
Speech is hybrid too: `faster-whisper` for command recognition, Microsoft Edge TTS
(no API key) with a local Piper fallback for the spoken answer. The brain runs on
`llm_provider: "auto"`, which silently switches between a cloud model (OpenRouter)
and local Ollama. Three entry points share the same brain: `--chat` (terminal),
`--chat-gui` (chat window) and `--voice` (tray + overlay).

## Highlights

- **Hybrid brain**: `core/llm_router.py` picks OpenRouter or Ollama per call, and
  drops to local for a cooldown window after a cloud failure. The local model is
  never loaded into VRAM/RAM while the cloud answer succeeds, so day-to-day use
  needs no multi-GB download.
- **Plugin architecture**: every capability (filesystem, web, Windows control, ...)
  is a self-contained module registered via `@register_tool` — adding a 101st tool
  never touches the dispatcher or the core loop. 39 tools ship today, across
  `filesystem.*`, `windows.*`, `browser.*`, `mouse_keyboard.*`, `memory.*`,
  `web.*`, `assistant.*` and `kule.*`.
- **Safety by construction**: each tool declares a `DangerLevel`; the dispatcher is
  the single choke point that enforces confirmation for anything destructive (see
  `utils/paths.py::safe_join` for the path-traversal guarding this buys — every
  filesystem tool routes its target through it).
- **Local speech recognition** via `faster-whisper`, no cloud STT dependency, with
  a continuous "Artemis" wake word (a `tiny` Whisper model) and a global hotkey
  (`ctrl+alt+a`) as the alternative trigger.
- **Multi-step planning**: `core/planner.py` sequences multi-step commands and
  halts the plan if a step fails or the user declines a confirmation, instead of
  ploughing ahead.
- **Persistent chat & settings GUI** (`ui/chat_window.py`, `ui/settings_window.py`)
  alongside the terminal `--chat`/`--voice` modes. The chat window keeps the whole
  conversation on screen as chat bubbles for as long as it is open.
- **Real DOM-level browser automation**: an own MCP server
  (`mcp_servers/browser_automation_server.py`) drives headless Chromium through
  Playwright — no third-party npm package, and its *test suite* needs no
  network. The tool itself navigates to whatever `url` it is given, so it does
  go out to the internet; by default it is restricted to `http(s)://` URLs on
  public hosts (see below).
- **812 automated tests** (2 more are marked `disruptive` and skipped by default)
  exercising real behavior — dispatcher, planner, filesystem safety, OpenRouter
  client, voice pipeline, UI — not mocks.

## Install & run

Requires Python 3.11+.

```bash
pip install -r requirements.txt      # or: pip install .
python scripts/setup_voice.py       # Piper TTS model, only needed for --voice

python main.py --chat               # text mode (terminal)
python main.py --chat-gui           # text mode, in a window
python main.py --settings           # settings window on its own
python main.py --voice              # voice assistant (faster-whisper + tray)
```

`--voice` additionally needs a speech model; `faster-whisper` downloads
`large-v3-turbo` (commands) and `tiny` (wake word) from Hugging Face on first use.

### The brain: cloud, local, or both

`config/config.yaml::llm_provider` decides, and the default `"auto"` needs neither
Ollama nor a heavy download:

- **`auto`** (default) — OpenRouter if `OPENROUTER_API_KEY` is set and reachable,
  otherwise local Ollama. After a cloud failure the router blocks the cloud for a
  cooldown window, so a dead connection is not retried on every command.
- **`cloud`** — OpenRouter only, silently never touching Ollama.
- **`local`** — Ollama only. Start it yourself, or let Artemis spawn it in the
  background: if the server is not running, Artemis starts it and shuts down only
  the instance *it* started. Installed models are listed and you pick by number,
  so there is nothing to write into `config.yaml` (the `ollama_model` field is just
  a fallback for non-interactive use).

```bash
ollama pull gemma4:e4b        # or whichever model you want to use
```

API keys are never read from `config.yaml` — that file is tracked by git. They are
read, in order, from the environment (`OPENROUTER_API_KEY`, `GROQ_API_KEY`,
`AZURE_SPEECH_KEY` + `AZURE_SPEECH_REGION`), then from `config/secrets.yaml`
(git-ignored), then from the Windows registry as a last resort.

If an IDE hard-kills the process (VS Code's Stop button), an orphaned `ollama`
process may survive; clean it up with:

```bash
python main.py --stop-ollama
```

### Browser automation (optional, off by default)

`pip install` only fetches the *Python package*; Chromium itself is a separate
download and is needed **only** for the browser automation server:

```bash
python -m playwright install chromium
```

To enable it, uncomment the `browser` entry under `mcp_servers:` in
`config/config.yaml` (it ships commented out, so the default install performs
zero I/O). `mcp_servers` is empty by default — with no server configured, that
section performs no I/O at all.

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

## Tests

```bash
pytest
```

Tests marked `disruptive` lock the screen and mute audio on the machine that runs
them, so they are excluded by default; run them explicitly only if you want that.

## Project layout

```
main.py            entry points (--chat, --chat-gui, --voice, --settings, --stop-ollama)
core/              dispatcher, planner, LLM clients + router, plugin loader, manifest
config/            Settings (pydantic) + config.yaml
plugins/           one file per capability (filesystem, web, windows, ...)
mcp_servers/       own MCP servers, e.g. Playwright browser automation
voice/             audio capture, STT, TTS, wake word, provider fallback router
ui/                PyQt6 chat window, overlay, tray, hotkey, settings, theme
memory/            SQLite-backed key-value context memory
tests/             812 tests covering the above
```

## Architecture deep-dive

The full design log — every architectural decision from v0.1 through the current
version, including two real bugs found and fixed during a security hardening pass
— lives in [`ARCHITECTURE.md`](./ARCHITECTURE.md).
