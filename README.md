# Artemis — Local Voice AI Assistant

A desktop assistant that controls your computer by voice, running entirely on a
**local LLM** (Ollama). The model never touches the OS directly — it can only emit a
JSON tool-call; a Python dispatcher validates and executes the real action. This
keeps the assistant auditable and lets dangerous operations (delete, shutdown, ...)
require explicit confirmation before anything runs.

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
- **540 automated tests** exercising real behavior (dispatcher, planner, rate
  limiting, filesystem safety) — not mocks.

## Install & run

```bash
pip install -r requirements.txt
ollama pull llama3.1          # or whichever model config.yaml points to
python main.py --chat         # text mode
python main.py --voice        # voice mode (faster-whisper)
```

Run the test suite with:

```bash
pytest
```

## Project layout

```
core/            dispatcher, planner, LLM client, plugin loader, manifest
config/          Settings (pydantic) + config.yaml
plugins/         one file per capability (filesystem, web, windows, ...)
memory/          SQLite-backed conversation memory
tests/           540 tests covering the above
```

## Architecture deep-dive

The full design log — every architectural decision from v0.1 through the current
version, including two real bugs found and fixed during a security hardening pass
— lives in [`ARCHITECTURE.md`](./ARCHITECTURE.md).
