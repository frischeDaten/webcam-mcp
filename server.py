"""webcam-mcp — local MCP server exposing the machine's webcam to Claude.

Runs natively on Windows (DirectShow via OpenCV). stdio transport only:
nothing is exposed on the network, and a frame is captured only when a
tool is explicitly called.

Tools
  list_cameras   probe camera indices, report which open, how they look, and
                 which one the auto-selector would use
  select_camera  override the auto-selection for this server session
  capture_frame  grab one still, return it as an image + save a JPEG to disk
  capture_burst  grab N stills spaced by an interval, return all of them

Camera selection
  Device indices are machine-specific and the right camera differs from host to
  host, so no index is hardcoded. Unless WEBCAM_DEVICE pins one, the first tool
  call probes the available indices and picks the best channel by what it
  actually delivers: a live picture beats a frozen one, a frozen one beats a
  black "no signal" frame, higher resolution and contrast break near ties, and
  a channel that merely mirrors another channel is demoted so the original
  wins. Nothing is probed at startup; probing happens on demand.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

try:  # keep OpenCV's backend probing chatter off stderr
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
except Exception:
    pass

# ---------------------------------------------------------------------------
# configuration (overridable via env in the desktop app's MCP config)
# ---------------------------------------------------------------------------
OUTPUT_DIR = Path(os.environ.get("WEBCAM_OUTPUT_DIR", Path.home() / "Pictures" / "webcam-mcp"))
DEFAULT_WIDTH = int(os.environ.get("WEBCAM_WIDTH", "1280"))
DEFAULT_HEIGHT = int(os.environ.get("WEBCAM_HEIGHT", "720"))
JPEG_QUALITY = int(os.environ.get("WEBCAM_JPEG_QUALITY", "85"))
WARMUP_FRAMES = int(os.environ.get("WEBCAM_WARMUP_FRAMES", "8"))
MAX_PROBE = int(os.environ.get("WEBCAM_MAX_PROBE", "6"))
# frames sampled per index while probing; virtual cameras can serve 1 fps, so
# keep this small or discovery takes tens of seconds
PROBE_FRAMES = int(os.environ.get("WEBCAM_PROBE_FRAMES", "3"))
# how long a single camera may take to deliver its first non-black frame
FIRST_FRAME_TIMEOUT = float(os.environ.get("WEBCAM_FIRST_FRAME_TIMEOUT", "4.0"))
# spacing between the frames used to estimate "is this picture moving at all"
PROBE_INTERVAL = float(os.environ.get("WEBCAM_PROBE_INTERVAL", "0.15"))

# Diagnostic log. stderr is the normal channel, but the desktop app does not
# keep it in this build, so every line is mirrored to a file as well. Set
# WEBCAM_LOG_FILE to another path to move it, or "off" to keep stderr only.
_LOG_ENV = (os.environ.get("WEBCAM_LOG_FILE") or "").strip()
if _LOG_ENV.lower() in ("off", "none", "0", "false"):
    LOG_PATH: Path | None = None
elif _LOG_ENV:
    LOG_PATH = Path(_LOG_ENV).expanduser()
else:
    LOG_PATH = OUTPUT_DIR / "webcam-mcp.log"
LOG_MAX_BYTES = 1_000_000

# WEBCAM_DEVICE: "auto" (default) resolves the best camera on first use; any
# integer pins that index. A pinned index that will not open raises a clear
# ToolError naming the index, rather than silently capturing the wrong camera.
_DEVICE_ENV = (os.environ.get("WEBCAM_DEVICE") or "auto").strip().lower()
PINNED_DEVICE: int | None = int(_DEVICE_ENV) if _DEVICE_ENV.isdigit() else None

BACKEND = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY

# selection heuristics
MIN_MOTION = 0.02      # mean |frame n - frame n-1| below this => frozen picture
DARK_MEAN = 4.0        # a frame this dark ...
DARK_STD = 3.0         # ... and this flat carries no picture at all
MIRROR_DIFF = 2.0      # two channels this close are the same source twice
STATE_SCORE = {"live": 100.0, "static": 40.0, "no-signal": 0.0, "no-frames": 0.0, "unavailable": -1.0}

class _MethodLogger:
    """Records inbound MCP requests so a session can be audited afterwards.

    The desktop app shows every call, but it keeps no copy of this server's
    stderr in the current build; mirroring each request into the log file is
    what makes "did the app connect, and what did it ask for?" answerable."""

    async def __call__(self, ctx, call_next):
        method = str(ctx.method)
        if not method.startswith("notifications/") and method != "ping":
            if method == "tools/call":
                name = str((ctx.params or {}).get("name", "?"))
                _log(f"request tools/call {name}")
            else:
                _log(f"request {method}")
        return await call_next(ctx)


mcp = MCPServer("webcam", middleware=[_MethodLogger()])

# resolved lazily on first use; {"index": int | None, "probes": list[dict] | None}
_selection: dict = {"index": None, "probes": None}


def _log(msg: str) -> None:
    # stdout is the MCP channel; diagnostics go to stderr (and the log file)
    print(f"[webcam-mcp] {msg}", file=sys.stderr, flush=True)
    if LOG_PATH is None:
        return
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".1"))
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{os.getpid()}] {msg}\n")
    except OSError:
        pass  # never let logging break a capture


def _open(device: int, width: int, height: int) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(device, BACKEND)
    if not cap.isOpened():
        cap.release()
        raise ToolError(
            f"Camera index {device} could not be opened. "
            "Check that no other app (Teams, Zoom, browser) holds it, and that "
            "Windows Settings > Privacy > Camera allows desktop apps. "
            "Run list_cameras to see which indices exist on this machine."
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


def _read_settled(cap: cv2.VideoCapture, warmup: int = WARMUP_FRAMES):
    """Discard the first frames so auto-exposure / white balance settle."""
    frame = None
    for _ in range(max(1, warmup)):
        ok, frame = cap.read()
        if not ok:
            frame = None
    if frame is None:
        raise ToolError("Camera opened but returned no frames.")
    return frame


def _encode(frame) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ToolError("JPEG encoding failed.")
    return buf.tobytes()


def _save(data: bytes, tag: str = "") -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = OUTPUT_DIR / f"webcam-{stamp}{tag}.jpg"
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# camera discovery / selection
# ---------------------------------------------------------------------------
def _probe(index: int) -> dict:
    """Open one index and describe what it actually delivers."""
    info: dict = {
        "index": index,
        "state": "unavailable",
        "width": 0,
        "height": 0,
        "frames": 0,
        "brightness": 0.0,
        "contrast": 0.0,
        "motion": 0.0,
        "thumb": None,
        "mirror_of": None,
    }
    cap = cv2.VideoCapture(index, BACKEND)
    try:
        if not cap.isOpened():
            return info
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, DEFAULT_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, DEFAULT_HEIGHT)

        # 1) wait for a first frame that actually carries a picture: capture
        #    cards and virtual cameras can serve black for a second or two
        #    after open, and judging those first frames would misclassify them
        last = None
        deadline = time.monotonic() + FIRST_FRAME_TIMEOUT
        while time.monotonic() < deadline:
            ok, frame = cap.read()
            if ok and frame is not None:
                info["frames"] += 1
                last = frame
                if float(frame.mean()) >= DARK_MEAN or float(frame.std()) >= DARK_STD:
                    break
            time.sleep(0.02)
        if last is None:
            info["state"] = "no-frames"
            return info

        info["height"], info["width"] = last.shape[:2]
        info["brightness"] = round(float(last.mean()), 1)
        info["contrast"] = round(float(last.std()), 1)
        if info["brightness"] < DARK_MEAN and info["contrast"] < DARK_STD:
            # black for the whole startup window: no point sampling motion
            info["state"] = "no-signal"
            return info

        # 2) sample a few frames spaced in time: enough to tell a live picture
        #    from a frozen one without waiting for slow devices to keep up
        prev = last
        motion_sum = 0.0
        motion_n = 0
        for _ in range(max(2, PROBE_FRAMES)):
            time.sleep(PROBE_INTERVAL)
            ok, frame = cap.read()
            if not ok or frame is None or frame.shape != prev.shape:
                continue
            info["frames"] += 1
            motion_sum += float(cv2.absdiff(frame, prev).mean())
            motion_n += 1
            prev = frame
            last = frame

        info["height"], info["width"] = last.shape[:2]
        info["brightness"] = round(float(last.mean()), 1)
        info["contrast"] = round(float(last.std()), 1)
        info["motion"] = round(motion_sum / motion_n, 3) if motion_n else 0.0
        # small grayscale thumbnail: used to spot channels that mirror each other
        info["thumb"] = cv2.resize(cv2.cvtColor(last, cv2.COLOR_BGR2GRAY), (32, 32)).astype("float32")
        info["state"] = "static" if info["motion"] < MIN_MOTION else "live"
    except Exception as exc:  # a flaky driver must not kill the whole probe
        info["state"] = "unavailable"
        info["note"] = str(exc)
    finally:
        cap.release()
    return info


def _score(info: dict) -> float:
    base = STATE_SCORE.get(str(info["state"]), 0.0)
    if base <= 0:
        return base
    pixels = min(int(info["width"]), 1920) * min(int(info["height"]), 1080)
    res = pixels / (1920 * 1080)
    contrast = min(float(info["contrast"]), 60.0) / 60.0
    score = base + res * 10.0 + contrast * 2.0
    if info["mirror_of"] is not None:
        score -= 15.0  # prefer the original channel over a mirror of it
    return score


def _mark_mirrors(probes: list[dict]) -> None:
    """Flag channels whose picture equals an earlier channel's picture."""
    seen: list[dict] = []
    for info in probes:
        if info["thumb"] is None or STATE_SCORE.get(str(info["state"]), 0.0) <= 0:
            continue  # black/absent channels carry no picture to compare
        for earlier in seen:
            diff = float(cv2.absdiff(info["thumb"], earlier["thumb"]).mean())
            if diff < MIRROR_DIFF:
                info["mirror_of"] = earlier["index"]
                break
        seen.append(info)


def _discover(max_index: int = MAX_PROBE) -> list[dict]:
    probes = [_probe(i) for i in range(max(1, max_index))]
    _mark_mirrors(probes)
    return probes


def _pick(probes: list[dict]) -> int | None:
    usable = [p for p in probes if STATE_SCORE.get(str(p["state"]), 0.0) > 0]
    if not usable:
        return None
    best = max(usable, key=lambda p: (_score(p), -int(p["index"])))
    return int(best["index"])


def _quick_pick() -> tuple[int | None, list[dict]]:
    """Cheap selection used on the first capture: walk the indices in order and
    stop at the first one that delivers a live picture, falling back to the
    first one that delivers anything at all. Usually finishes in a few seconds
    instead of probing every index; list_cameras/select_camera() do the full
    scored scan when the exact ranking matters."""
    probes: list[dict] = []
    fallback: int | None = None
    for index in range(max(1, MAX_PROBE)):
        info = _probe(index)
        probes.append(info)
        if str(info["state"]) == "live":
            return index, probes
        if fallback is None and STATE_SCORE.get(str(info["state"]), 0.0) > 0:
            fallback = index
    return fallback, probes


def _commit(probes: list[dict], chosen: int | None) -> int:
    """Cache `chosen` as the session camera, or explain why there is none."""
    _selection["probes"] = probes
    if chosen is None:
        raise ToolError(
            "No working camera found on indices 0..%d. Check that Windows "
            "Settings > Privacy & security > Camera allows desktop apps, and "
            "that no other app (Teams, Zoom, browser) holds the camera."
            % max(0, len(probes) - 1)
        )
    _selection["index"] = chosen
    picked = probes[chosen]
    _log(
        "auto-selected camera index %d (%sx%s, %s)"
        % (chosen, picked["width"], picked["height"], picked["state"])
    )
    return chosen


def _resolve(device: int | None = None) -> int:
    """Return the camera index to use, discovering and caching it if needed."""
    if device is not None:
        return int(device)

    if PINNED_DEVICE is not None:
        return PINNED_DEVICE

    if _selection["index"] is not None:
        return int(_selection["index"])

    chosen, probes = _quick_pick()
    return _commit(probes, chosen)


def _describe(info: dict) -> str:
    state = str(info["state"])
    if state == "unavailable":
        return "not available"
    if state == "no-frames":
        return "opens but delivers no frames"
    text = "%s, %sx%s, brightness %.0f, contrast %.0f, motion %.2f" % (
        state, info["width"], info["height"], info["brightness"], info["contrast"], info["motion"]
    )
    if info["mirror_of"] is not None:
        text += ", mirrors index %d" % info["mirror_of"]
    if state == "no-signal":
        text += " (black frame - device open but no picture)"
    elif state == "static":
        text += " (frozen picture - placeholder screen or still source)"
    return text


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_cameras(max_index: int = MAX_PROBE) -> str:
    """Probe camera indices 0..max_index-1 and report, per index, whether it
    opens and what it delivers (resolution, brightness, motion, whether it only
    mirrors another index), plus which index the auto-selector would use. Call
    this when a capture looks wrong, or to choose a specific device yourself."""
    probes = _discover(max_index)
    _selection["probes"] = probes
    chosen = _pick(probes)
    lines = [f"index {p['index']}: {_describe(p)}" for p in probes]
    if chosen is None:
        lines.append("")
        lines.append("No usable camera found (indices 0..%d)." % (max(1, max_index) - 1))
        lines.append(
            'Check Windows Settings > Privacy & security > Camera > "Let desktop apps '
            'access your camera" and close any app holding the camera.'
        )
    else:
        lines.append("")
        lines.append(f"Auto-selected device: {chosen} (used when no device is passed)")
        if PINNED_DEVICE is not None:
            lines.append(f"WEBCAM_DEVICE is set, so device {PINNED_DEVICE} is always used instead.")
        lines.append("Override for this session with select_camera(device=N).")
    lines.append(f"Output dir: {OUTPUT_DIR}")
    return "\n".join(lines)


@mcp.tool()
def select_camera(device: int | None = None) -> str:
    """Pick the camera for the rest of this server session. device=None
    re-runs the auto-selection (or returns to it after a manual override).
    Use list_cameras first to see the indices and what they deliver."""
    if device is None:
        # full scored scan, not the cheap first-live shortcut
        probes = _discover()
        chosen = _commit(probes, _pick(probes))
        return "Auto-selection restored: using device %d (%s)." % (
            chosen, _describe(probes[chosen])
        )

    device = int(device)
    if not 0 <= device <= 63:
        raise ToolError(f"device {device} is not a plausible camera index (0..63).")
    cap = cv2.VideoCapture(device, BACKEND)
    try:
        if not cap.isOpened():
            raise ToolError(
                f"Camera index {device} could not be opened, so it was not selected. "
                "Run list_cameras to see which indices exist."
            )
    finally:
        cap.release()
    _selection["index"] = device
    _log(f"camera index {device} selected explicitly")
    return f"Using device {device} for this session. Pass device=None to go back to auto-selection."


@mcp.tool()
def capture_frame(
    device: int | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    rotate: int = 0,
    save: bool = True,
) -> list[Image | str]:
    """Capture a single still from the webcam and return it as an image so
    Claude can look at what is on the desk / in front of the camera.

    device: camera index, or omit for the auto-selected camera (run
    list_cameras / select_camera to see or change it). width/height: requested
    resolution (camera may pick nearest supported). rotate: 0, 90, 180 or 270
    degrees clockwise. save: also write a timestamped JPEG to the output dir."""
    index = _resolve(device)
    cap = _open(index, width, height)
    try:
        frame = _read_settled(cap)
    finally:
        cap.release()

    if rotate in (90, 180, 270):
        code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate]
        frame = cv2.rotate(frame, code)

    data = _encode(frame)
    h, w = frame.shape[:2]
    note = f"Captured {w}x{h} from device {index} at {datetime.now():%Y-%m-%d %H:%M:%S}"
    if device is None:
        note += " (session default camera)"
    if save:
        path = _save(data)
        note += f"; saved to {path}"
    _log(note)
    return [Image(data=data, format="jpeg"), note]


@mcp.tool()
def capture_burst(
    count: int = 3,
    interval_seconds: float = 2.0,
    device: int | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    save: bool = True,
) -> list[Image | str]:
    """Capture several stills spaced by interval_seconds, keeping the camera
    open in between. Use when Claude should watch something change over a
    short period (max 10 frames, max 30 s interval). device: omit for the
    auto-selected camera."""
    count = max(1, min(count, 10))
    interval_seconds = max(0.2, min(interval_seconds, 30.0))

    index = _resolve(device)
    cap = _open(index, width, height)
    out: list[Image | str] = []
    try:
        _read_settled(cap)  # warm up once
        for i in range(count):
            if i:
                # keep draining frames so the next read is current, not buffered
                t_end = time.monotonic() + interval_seconds
                while time.monotonic() < t_end:
                    cap.grab()
                    time.sleep(0.03)
            ok, frame = cap.read()
            if not ok:
                out.append(f"frame {i + 1}: read failed")
                continue
            data = _encode(frame)
            stamp = datetime.now().strftime("%H:%M:%S")
            note = f"frame {i + 1}/{count} at {stamp} from device {index}"
            if save:
                note += f" -> {_save(data, tag=f'-{i + 1:02d}')}"
            out.append(Image(data=data, format="jpeg"))
            out.append(note)
    finally:
        cap.release()
    return out


def main() -> None:
    if PINNED_DEVICE is not None:
        device_note = f"device {PINNED_DEVICE} (pinned via WEBCAM_DEVICE)"
    else:
        device_note = "camera auto-selected on first capture"
    _log(
        "starting; %s, output %s, python %s, opencv %s"
        % (device_note, OUTPUT_DIR, sys.version.split()[0], cv2.__version__)
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
