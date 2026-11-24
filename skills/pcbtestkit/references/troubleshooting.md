# Troubleshooting

## Tool error codes (`"ok": false`)

| Code | Meaning | Do |
|---|---|---|
| `NO_PROJECT` | No board open | Pass `--project P` (CLI) or call `open_project` (MCP). |
| `UNKNOWN_NET`, `UNKNOWN_REF`, `UNKNOWN_FINDING`, `UNKNOWN_STEP`, `UNKNOWN_CHECK`, `UNKNOWN_CLASS_SYSTEM` | Name not on this board / in the registry | Use the `did_you_mean` value, or list with `list_nets` / `list_components` / `list_findings` / `list_class_systems`. |
| `MISSING_ARGUMENT`, `UNKNOWN_ARGUMENT`, `BAD_TYPE`, `BAD_VALUE` | Arguments don't match the schema | `pcbtestkit tools --name <tool>`. |
| `BAD_LIMITS`, `OUT_OF_RANGE` | Implausible limits (min >= max, tolerance outside 0.1-50 %, ...) | Fix the numbers. |
| `CONFIRM_REQUIRED` | The write conflicts with what the board shows (e.g. +3V3 set to 5 V, supply > 60 V) | Resend with `"confirm": true` only if intended. |
| `NET_NOT_PROBED` | A test step measures a net without a probe channel | Follow the hint: include the net (`set_test_options include_nets`) or add a test pad. |
| `INVALID_CONFIG` | The change would make the plan fail | Read the message; the change was not stored. |
| `INVALID_DEFINITION`, `DUPLICATE_ID` | Class-system definition rejected by the validator | Fix the listed errors; replacing a built-in needs `confirm`. |
| `NOTHING_TO_UNDO`, `NOTHING_TO_DO` | No earlier write / nothing would change | Nothing to fix. |
| `NOT_GENERATED`, `BAD_POINTER` | `read_output` before `run`, or a wrong JSON pointer | Run `pcbtestkit run P` first; check the pointer map in outputs.md. |
| `NOT_IN_PROFILE`, `READ_ONLY` (MCP only) | The server was started with `--profile core`/`readonly` or `--read-only` | Restart it with `--profile full` / without `--read-only`. |

## Environment

| Symptom | Cause | Fix |
|---|---|---|
| Renders, DRC/ERC or video skipped: "kicad-cli not found" | KiCad not installed or not found | Install KiCad 8+ (DRC/ERC) or 9+ (3D renders and video), or set `KICAD_CLI=/path/to/kicad-cli`. Everything else still works. |
| "video skipped: ffmpeg not found" | No ffmpeg | Install ffmpeg (`brew install ffmpeg`, `apt install ffmpeg`, `winget install ffmpeg`) or set `FFMPEG`. |
| "no Python with wxPython found" | The video's captions need KiCad's bundled Python | Install KiCad 9+; it ships wxPython. |
| Video takes long | Every frame is a KiCad render (about 0.5-1 s each, in parallel) | `--video-fps 15`, or `--video-stills` for a stills-only video in under a minute. |
| `python3` not found (Windows) | Windows uses `py -3` or `python` | Replace `python3` with `py -3`. |
| Plugin button missing in KiCad | Plugin list not refreshed | PCB Editor: Tools > External Plugins > Refresh Plugins, or restart KiCad. |
| Two PCB TestKit buttons | Installed both by the Plugin Manager and by `install.py` | Remove one: `python3 install.py --uninstall`, or uninstall in the Plugin Manager. |
| User class system not found | Wrong folder | `~/.pcbtestkit/classsystems/`, `<project>/classsystems/`, or `$PCBTESTKIT_CLASSSYSTEMS` (path-separated list). |
