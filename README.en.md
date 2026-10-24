# webagent

> Automate any web page with **any OpenAI-compatible model**: screenshot → OCR/vision → model planning → real mouse & keyboard → verify → loop.

[中文](README.md) | English

![Demo page](docs/demo.png)

<p>
  <img alt="CI" src="https://github.com/10086114514-ship-it/webagent/actions/workflows/ci.yml/badge.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-blue">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-lightgrey">
</p>

A desktop-level GUI agent for Windows. It does **not** use browser extensions, does **not**
modify pages, and does **not** inject JavaScript — it looks at the screen and moves the mouse,
just like a human. That makes it work with *any* web UI, including intranet systems, legacy
back offices, and sites that expose no API.

---

## Contents

- [What it does](#what-it-does)
- [Features](#features)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Supported models](#supported-models)
- [CLI reference](#cli-reference)
- [Action protocol](#action-protocol)
- [Engineering notes](#engineering-notes)
- [Project layout](#project-layout)
- [Development & testing](#development--testing)
- [FAQ](#faq)
- [Limitations](#limitations)
- [Usage boundary](#usage-boundary)
- [License](#license)

## What it does

Give it one sentence, for example:

> "This is an online quiz. Pick the correct answer for each question, fill in the blanks,
> then click Next until you are done."

`webagent` loops through:

```
screenshot → OCR / image → LLM → JSON action plan → execute → re-screenshot & verify → repeat
```

Typical uses: web automation testing, accessibility assistance, automating repetitive
web work that has no API, and GUI-agent research.

## Features

- **Model-agnostic**: any service exposing `POST /chat/completions` works (DeepSeek, OpenAI,
  Moonshot, Qwen, Groq, local Ollama, …). You only change `base_url` + `api_key` + `model`.
- **Vision or text-only**: vision models read the screenshot directly; text-only models get
  OCR text plus normalized bounding boxes and can still click precisely.
- **No heavy dependencies**: GDI for capture, `SendInput` for input, the built-in Windows OCR
  engine, and stdlib `urllib` for API calls. The only third-party dependency is Pillow.
- **Safe by default**: `dry-run` unless you explicitly pass `--live`.
- **Loop protection**: after every action the page fingerprint is compared; if nothing changed,
  the model is told to try something else, and the run stops after repeated no-ops.
- **Observable**: every plan is logged and screenshots are dumped to `debug/`.

## How it works

```
┌──────────────┐  1. find window by title (DPI-aware, restores if minimized)
│   Browser    │  2. GDI BitBlt the client area (physical pixels)
└──────┬───────┘
       │  3. Windows OCR → words + boxes → grouped into lines / segments
       ▼
┌────────────────────────────────────────────────────┐
│ Build the context                                  │
│  · task instruction (agent.instruction)            │
│  · current state (e.g. "selected C", "filled 北京") │
│  · interactive elements (options / buttons / input │
│    with exact bounding boxes)                      │
│  · visible text, each line with its normalized bbox│
└──────┬─────────────────────────────────────────────┘
       │  4. call the model (text and/or image)
       ▼
┌────────────────────────────────────────────────────┐
│ JSON action plan                                   │
│ {"done":false,"actions":[                          │
│   {"type":"click","bbox":[x1,y1,x2,y2]},           │
│   {"type":"type","bbox":[…],"text":"Beijing"}]}    │
└──────┬─────────────────────────────────────────────┘
       │  5. normalized coords × window size → physical pixels → click / paste
       ▼
┌────────────────────────────────────────────────────┐
│ 6. verify: re-capture, compare fingerprints        │
│  · changed     → continue                          │
│  · unchanged   → tell the model to change tactics  │
│  · type action → check the text really appears     │
└──────┬─────────────────────────────────────────────┘
       │  done=true or max_steps reached
       ▼
   finish and report `result`
```

## Quick start

### 1. Requirements

- Windows 10/11
- Python 3.9+
- An API key for any OpenAI-compatible service

```bash
git clone https://github.com/10086114514-ship-it/webagent.git
cd webagent
python -m pip install -r requirements.txt
copy config.example.json config.json
```

### 2. Add your API key

```bash
python -m webagent key
# paste the key when prompted
```

Or set an environment variable: `set DEEPSEEK_API_KEY=sk-xxx`

### 3. Open the target page

```bash
python -m webagent windows   # list window titles
python -m webagent doctor    # check that the target window is found
```

Adjust `target.window_title` in `config.json` if needed (substring match).

### 4. Preview, then run

```bash
python -m webagent run --dry-run   # plan only, no clicking
python -m webagent run --live      # actually execute
python -m webagent watch --live    # keep looping (F8 pause, F9 run once, Esc quit)
```

On Windows you can also use `run.bat run --live`.

### Try the built-in demo

```bash
python -m webagent demo                    # opens demo/quiz.html (title "在线测验")
python -m webagent run --mock-live --force # offline: verifies capture → locate → click
```

## Configuration

`config.json` (copy from `config.example.json`):

| Field | Description | Default |
| --- | --- | --- |
| `target.window_title` | Browser tab title (the page `<title>`), substring match | `在线测验` |
| `target.window_match` | `contains` / `regex` / `exact` | `contains` |
| `target.client_area` | Capture the page viewport only | `true` |
| `target.activate` | Bring the window to front before acting | `true` |
| `model.base_url` | OpenAI-compatible endpoint | DeepSeek |
| `model.api_key` | API key (empty → read env var) | `""` |
| `model.api_key_env` | Env var holding the key | `DEEPSEEK_API_KEY` |
| `model.model` | Model name | `deepseek-chat` |
| `model.vision` | Whether the model accepts images | `false` |
| `model.mock` | Offline mode with a scripted plan | `false` |
| `ocr.enabled` | Run OCR (required for text-only models) | `true` |
| `ocr.language` | Windows OCR language tag | `zh-Hans` |
| `ocr.scale` | Upscale before OCR (1.5–2 for small text) | `1.0` |
| `agent.instruction` | Task prompt; empty means the built-in default | `""` |
| `agent.max_steps` | Max actions per run | `20` |
| `agent.step_delay_s` | Delay between steps | `0.5` |
| `agent.cooldown_s` | Ignore identical page states within this window | `2.0` |
| `action.type_mode` | `paste` (clipboard, reliable for CJK) / `unicode` | `paste` |
| `action.click_delay_ms` | Wait after a click | `120` |
| `runtime.interval_s` | Watch-mode sampling interval | `2.0` |
| `runtime.debug_dir` | Screenshot output directory | `debug` |

## Supported models

| Provider | `base_url` | `model` | `vision` |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | false |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | true |
| Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | false |
| Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` / `qwen-vl-max` | vl models |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | false |
| Ollama (local) | `http://localhost:11434/v1` | `qwen2.5` / `llama3.2-vision` | depends |

> Text-only models work too: OCR ships each visible line with a normalized bbox, and the
> model can reference those boxes directly when clicking.

## CLI reference

| Command | Purpose |
| --- | --- |
| `python -m webagent init` | Write a default `config.json` |
| `python -m webagent key [KEY]` | Store the API key without editing files |
| `python -m webagent doctor` | Environment self-check |
| `python -m webagent windows` | List window titles (including minimized) |
| `python -m webagent snapshot --lines` | Capture + OCR preview for debugging |
| `python -m webagent demo` | Open the bundled demo page |
| `python -m webagent run [--live] [--dry-run] [--mock] [--force] [--instruction ...]` | One run |
| `python -m webagent watch [--live]` | Continuous loop with global hotkeys |

## Action protocol

The model must return a single JSON object (markdown fences and surrounding chatter are tolerated):

```json
{
  "done": false,
  "reasoning": "Answer is A, clicking option A",
  "actions": [
    {"type": "click", "bbox": [0.295, 0.447, 0.350, 0.467], "reason": "option A"}
  ],
  "result": null
}
```

- `bbox`: `[x1,y1,x2,y2]`, normalized to `0..1` over the captured screenshot.
- Action types: `click`, `type`, `scroll`, `key`, `wait`.
- Set `done: true` and put the outcome in `result`.

## Engineering notes

These are the real problems hit while building this, and the parts of the code worth reading:

1. **DPI consistency** — the process declares Per-Monitor DPI awareness at startup so that
   `GetWindowRect`, GDI capture, and `SetCursorPos` all use the same physical-pixel space.
   Without it, every click is off at 150% scaling.
2. **OCR cannot express selection state** — a filled vs. empty radio button looks identical in
   text, so the model kept clicking the same option forever. The agent now infers page state
   (`selected C`, `filled 北京`) and tells the model to click *Next* instead.
3. **Merged lines cause mis-clicks** — if `selected C  Next` becomes one large bbox, clicking its
   center hits empty space. Bounding boxes are therefore emitted **per text segment**.
4. **64-bit handle truncation** — `GlobalAlloc`/`GlobalLock` return values truncated to `c_int`
   crash the clipboard write. Declaring `c_void_p` return types fixed CJK text input entirely.
5. **No-change detection** — after each action the page fingerprint is compared; repeated no-ops
   produce a warning and eventually stop the run.
6. **Minimized windows** — `GetWindowRect` returns `-21333` for minimized windows; the agent uses
   `GetWindowPlacement` and restores the window automatically.

## Project layout

```
webagent/
├── webagent/
│   ├── __main__.py     # CLI: init/doctor/key/windows/snapshot/run/watch/demo
│   ├── agent.py        # context building, model calls, plan parsing, execution, verification
│   ├── capture.py      # GDI capture, window lookup, DPI awareness
│   ├── ocr.py          # Windows OCR wrapper (PowerShell WinRT projection)
│   ├── ps_ocr.ps1      # the OCR script itself
│   ├── models.py       # OpenAI-compatible client (urllib, vision support)
│   ├── actions.py      # mouse / keyboard / wheel / clipboard via SendInput
│   ├── hotkeys.py      # global hotkey polling
│   └── config.py       # JSON config loading and merging
├── demo/quiz.html      # bundled demo page (works offline)
├── tools/mock_server.py# local fake model server for offline testing
├── tests/              # unit tests
├── docs/demo.png       # demo screenshot
├── config.example.json # config template (no secrets)
├── run.bat             # Windows launcher
└── pyproject.toml
```

## Development & testing

```bash
python -m unittest discover -s tests -v
```

Test the model plumbing offline with the bundled fake server:

```bash
python tools/mock_server.py --port 8799 --plan-file tools/plan.json --log tools/requests.jsonl
python -m webagent run --config tools/test_config.json --dry-run
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## FAQ

**"Window not found"** — open the page first, run `python -m webagent windows`, then set
`target.window_title` to a distinctive part of the title.

**Clicks land in the wrong place** — make sure the browser and the agent run on the same
monitor at the same scaling; DPI is handled automatically.

**OCR finds no text** — Chinese OCR needs the Chinese language pack installed (`doctor` reports
OCR status). For small text, try `ocr.scale = 1.5`.

**The model keeps clicking the same option** — the agent already reports the "selected" state;
if the model ignores it, use a more instruction-following model or lower `temperature`.

**Typing Chinese does nothing** — keep `action.type_mode = "paste"`; switch to `"unicode"` only
if the target app blocks clipboard paste.

**The model returns non-JSON** — the agent strips code fences and extracts `{...}`; if it still
fails, set `temperature` to 0.

## Limitations

- Windows only (GDI / SendInput / Windows OCR).
- Coordinates depend on OCR and the model; overlays, dynamic popups, and canvas-heavy UIs may
  confuse it.
- One window at a time; no multi-tab switching.
- One model call per action, so speed tracks model latency (roughly 1–3 s per step).

## Usage boundary

This is a **general-purpose web automation framework** that faithfully executes whatever you
write in `agent.instruction`. Use it only on pages you are authorized to automate and where it
does not violate the platform's rules or any exam/academic-integrity policy. Do not use it to
bypass proctoring, take exams for someone else, or commit fraud. Your API key is your own
responsibility — never commit it.

## License

[MIT](LICENSE)
