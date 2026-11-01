# MCP server: webcam

A local [MCP](https://modelcontextprotocol.io) server that gives an assistant eyes on the room: `list_cameras`, `select_camera`, `capture_frame` and `capture_burst` over stdio.

Windows-native (OpenCV over DirectShow, falling back to the platform backend elsewhere). Nothing listens on the network, no frame is captured until a tool is explicitly called, and the host app shows every call.

Built originally for the Claude desktop app; **any MCP client that can run a stdio command works** — Claude Code, VS Code / Copilot, Cursor, Continue, a custom harness. See [Register with your client](#register-with-your-client).

## Why another camera server

Camera indices are machine-specific, and a typical Windows box exposes several channels that *open fine and return frames* but show nothing useful: virtual cameras stuck on a "connect the app" splash, NVIDIA Broadcast or OBS output with no active source (black), a capture card that is not passing a signal. Hardcoding `device: 0` therefore works on one machine and shows a splash screen on the next.

This server has no index compiled in. On the first capture it probes the channels and ranks them by what they actually deliver:

1. a **live** picture beats a **frozen** one, and a frozen one beats a **black** frame (device open, no picture)
2. higher resolution and contrast break near ties
3. a channel that is an exact duplicate of an earlier channel is demoted, so the original wins

The result is cached for the session. `list_cameras` prints that reasoning in plain text, and `select_camera(device=N)` or `WEBCAM_DEVICE=<n>` overrides it. Probing happens on demand, never at startup.

## Tools

| Tool | What it does |
|---|---|
| `list_cameras` | Probes indices `0..max_index-1`, reports per index whether it opens and what it delivers (resolution, brightness, contrast, motion, whether it only mirrors another index), plus which index would be auto-selected |
| `select_camera` | Pins one index for the session; `select_camera()` goes back to auto-selection |
| `capture_frame` | One still -> returned as an image to the assistant + saved as JPEG |
| `capture_burst` | N stills at an interval (max 10 frames / 30 s) -> all returned |

`capture_frame`: `device` (omit for the auto-selected camera), `width`/`height` (requested; the camera picks the nearest supported mode), `rotate` (0/90/180/270), `save` (write a JPEG).

## Requirements

- Windows 10/11 (the installer script is PowerShell; the server itself also runs on macOS/Linux with the same code)
- Python 3.10+ (`py -3` or `python` on PATH)
- a camera, and camera permission for desktop apps: Settings -> Privacy & security -> Camera -> *Let desktop apps access your camera*

## Install

```powershell
git clone https://github.com/frischeDaten/webcam-mcp.git
cd webcam-mcp
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The script creates `.venv`, installs the dependencies, probes your cameras, and prints the config block for your copy:

```json
{
  "command": "C:\\path\\to\\webcam-mcp\\.venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\webcam-mcp\\server.py"],
  "env": { "WEBCAM_DEVICE": "auto" }
}
```

To let the script merge that into the Claude desktop config for you:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -RegisterClaudeDesktop
```

It backs the file up to `claude_desktop_config.json.bak-<yyyymmdd>`, adds exactly one `webcam` key, leaves every other entry untouched, and refuses to overwrite an existing `webcam` entry that points somewhere else. Restart the desktop app afterwards.

Manual install, any OS:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install "mcp>=2.0" "opencv-python>=4.9"   # .venv/bin/python on macOS/Linux
```

## Register with your client

Every MCP stdio client needs the same two pieces: the interpreter inside `.venv` as `command`, `server.py` as the single argument. Always use the venv interpreter - a bare `python` will not have `mcp` installed.

**Claude desktop app** - `%APPDATA%\Claude\claude_desktop_config.json`, under `mcpServers`:

```json
"webcam": {
  "command": "C:\\path\\to\\webcam-mcp\\.venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\webcam-mcp\\server.py"],
  "env": { "WEBCAM_DEVICE": "auto" }
}
```

**Claude Code**

```bash
claude mcp add webcam -- "C:\path\to\webcam-mcp\.venv\Scripts\python.exe" "C:\path\to\webcam-mcp\server.py"
```

**VS Code / Copilot** - `.vscode/mcp.json` (or the user-level `mcp.json`):

```json
{
  "servers": {
    "webcam": {
      "type": "stdio",
      "command": "C:\\path\\to\\webcam-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\webcam-mcp\\server.py"],
      "env": { "WEBCAM_DEVICE": "auto" }
    }
  }
}
```

**Cursor** - `~/.cursor/mcp.json`, same shape as the Claude desktop block above.

**Anything else** - spawn `command` with `args`, speak MCP over stdio. Examples live in [`examples/`](examples/).

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `WEBCAM_DEVICE` | `auto` | `auto` picks the best camera on first use; a number pins that index |
| `WEBCAM_WIDTH` / `WEBCAM_HEIGHT` | `1280` / `720` | requested capture resolution |
| `WEBCAM_JPEG_QUALITY` | `85` | 1-100 |
| `WEBCAM_WARMUP_FRAMES` | `8` | frames discarded so auto-exposure settles (raise to 15-20 if captures come out dark) |
| `WEBCAM_OUTPUT_DIR` | `%USERPROFILE%\Pictures\webcam-mcp` | where JPEGs and the log are written |
| `WEBCAM_MAX_PROBE` | `6` | how many indices discovery tries |
| `WEBCAM_PROBE_FRAMES` | `3` | frames sampled per index while probing (raise for a steadier live/frozen verdict) |
| `WEBCAM_FIRST_FRAME_TIMEOUT` | `4.0` | seconds a camera may take to deliver its first non-black frame |
| `WEBCAM_PROBE_INTERVAL` | `0.15` | seconds between the frames compared to detect motion |
| `WEBCAM_LOG_FILE` | `<output dir>\webcam-mcp.log` | diagnostic log path; `off` keeps stderr only |

## Logs

Many hosts keep no copy of an MCP server's stderr, so every diagnostic line is also appended to `<output dir>\webcam-mcp.log` (rotated at 1 MB). It records each inbound request - `request initialize`, `request tools/list`, `request tools/call capture_frame` - plus which camera the selector chose and every file saved. That is the quickest way to tell "the client never connected" apart from "the client called and the camera failed".

## Troubleshooting

- **"could not be opened"** - another app holds the camera (Teams, Zoom, browser tab), or Windows privacy blocks desktop apps: Settings -> Privacy & security -> Camera -> *Let desktop apps access your camera*.
- **Wrong camera** - run `list_cameras`; it explains what each index delivers. Then `select_camera(device=N)` for the session, or set `WEBCAM_DEVICE` to pin it permanently.
- **"No working camera found"** - every probed index was closed, black or frame-less. Check the privacy setting, close apps holding the camera, and raise `WEBCAM_MAX_PROBE` if the camera sits on a high index.
- **First capture is slow** - the first call probes the indices (a few seconds; cameras that serve 1 fps make it worse). Later calls reuse the cached choice. `list_cameras` is the slowest call by design.
- **Dark or green first frame** - raise `WEBCAM_WARMUP_FRAMES` to 15-20; some USB cameras are slow to settle.
- **Image is tiny** - the camera ignored the requested resolution; pass a `width`/`height` the device actually supports (1920x1080, 1280x720, 640x480).
- **Server not listed in the app** - validate the JSON (no trailing commas), make sure `command` points into `.venv`, and restart the app fully (tray -> Quit).

## Notes

- The camera is opened only for the duration of a call, then released, so other apps get it back immediately.
- `capture_burst` keeps the camera open but drains frames between shots, so the returned stills are current rather than buffered.
- Tested with `mcp 2.3.0` / `opencv-python 5.0.0.93` on Windows 11 with Python 3.12.

## License

MIT - see [LICENSE](LICENSE).
