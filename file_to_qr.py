#!/usr/bin/env python3
"""
File to QR — utility for cold offline storage of encrypted files in QR codes.

Runs in Termux on Android. Uses:
  - openssl CLI for AES-256-CBC encryption (pbkdf2)
  - qrencode CLI to generate QR codes
  - zbarimg to read QR codes from PNG/photos
  - termux-camera-photo for camera access (Mode 3)
  - rich (the only pip dependency) for the TUI

NO network calls whatsoever. Fully offline.
"""

import base64
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.prompt import Prompt, Confirm
    from rich.live import Live
except ImportError:
    print("❌ Library 'rich' not found. Install it: pip install rich")
    sys.exit(1)

try:
    from PIL import Image, ImageOps, ImageFilter
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

console = Console()

# ---------------------------------------------------------------------------
# Unified color/icon semantics for the UI
# ---------------------------------------------------------------------------
# Previously success/error/info messages were colored with literals ("red"/
# "green"/"blue") separately at each call site — colors and icons gradually
# drifted apart (❌ in some places, no icon in others; "blue" for info here,
# "cyan" there). Here's a single source of truth: use these helpers instead
# of bare console.print(f"[red]...[/red]") throughout the file, so the style
# is guaranteed to be consistent everywhere and can be fixed in one place.
_C_OK = "bright_green"      # success — same green as in the banner/menu
_C_ERR = "bold red"         # error
_C_WARN = "yellow"          # warning
_C_INFO = "cyan"            # neutral info/hint
_C_DIM = "bright_black"     # secondary/muted text
_C_ACCENT = "bright_cyan"   # sub-mode headers (instead of a mix of blue/cyan)


def _ok(msg: str):
    console.print(f"[{_C_OK}]✓ {msg}[/{_C_OK}]")


def _err(msg: str):
    console.print(f"[{_C_ERR}]❌ {msg}[/{_C_ERR}]")


def _warn(msg: str):
    console.print(f"[{_C_WARN}]⚠ {msg}[/{_C_WARN}]")


def _info(msg: str):
    console.print(f"[{_C_INFO}]{msg}[/{_C_INFO}]")


def _section_panel(title: str) -> Panel:
    """Unified sub-mode header — previously each menu_* built its own
    Panel("[bold blue]...") with mismatched colors and no padding; now all
    sub-mode headers use one style (bright_cyan, with padding), consistent
    with the main menu border (bright_green)."""
    return Panel(
        f"[bold {_C_ACCENT}]{title}[/bold {_C_ACCENT}]",
        border_style=_C_ACCENT,
        expand=False,
        padding=(0, 2),
    )


def _styled_table(title: str) -> Table:
    """Unified style for results/settings tables — previously Table(title=...)
    was created without border_style/padding and looked flatter than the
    panels next to it; now the border and header match the rest of the UI."""
    return Table(
        title=title,
        title_style=f"bold {_C_ACCENT}",
        border_style=_C_DIM,
        header_style=f"bold {_C_INFO}",
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_INPUT_FILE_BYTES = 52 * 1024  # 52 KB — hard limit on the source file

# Practical ceiling for the size of a raw (pre-base64) data chunk per QR code,
# chosen so the resulting QR version stays printable at 5x5 cm and reliably
# readable by a phone camera without macro mode.
# At error correction level M this corresponds to roughly QR version 25-27.
DEFAULT_CHUNK_SIZE = 1300  # bytes of raw data before base64 expansion

# Absolute ceiling (soft warning above this, hard cap below version 40)
PRACTICAL_MAX_CHUNK_SIZE = 1900  # bytes of raw data — roughly version 30-32 at M

TEMP_DIR = Path("/data/data/com.termux/files/usr/tmp")
if not TEMP_DIR.exists():
    # fallback for testing outside Termux
    TEMP_DIR = Path("/tmp")

CONFIG_PATH = Path.home() / ".file_to_qr_config.json"
# Default output folder — next to the script, not in the home folder.
SCRIPT_DIR = Path(__file__).resolve().parent

# Scan-speed diagnostics: FILE_TO_QR_DEBUG_TIMING=1 python3 file_to_qr.py
# prints to stderr how long capture/processing/recognition takes for each
# frame — useful if a scan seems slower than expected
# (e.g. for the auto-loop in Mode 5 or manual capture in Mode 3).
FILE_TO_QR_DEBUG_TIMING = os.environ.get("FILE_TO_QR_DEBUG_TIMING") == "1"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "file_to_qr_output"
DEFAULT_DOWNLOADS = Path.home() / "storage" / "downloads"

QR_PREFIX = "QRVAULT"


# ---------------------------------------------------------------------------
# Environment check
# ---------------------------------------------------------------------------

REQUIRED_BINARIES = {
    "openssl": "pkg install openssl-tool",
    "qrencode": "pkg install qrencode",
    "zbarimg": "pkg install zbar",
}

OPTIONAL_BINARIES = {
    "termux-camera-photo": "pkg install termux-api (and install the Termux:API app from F-Droid)",
}


# ---------------------------------------------------------------------------
# Visual feedback when operations finish (no sound/vibration)
# ---------------------------------------------------------------------------

def _fx_flash_success(text: str):
    """Short victory animation: the text blinks 3 times between a bright and
    a dim tone before staying on screen permanently. Used when a large
    operation finishes (e.g. all QR chunks have been collected), so the
    moment is visually more noticeable than a single line of text.

    Best-effort — if Live doesn't render on a non-standard terminal, it just
    prints the text once statically and moves on.
    """
    try:
        # refresh_per_second used to be 8 — lower than the rate of our own
        # time.sleep(0.12-0.18) frame switches, which caused Live to
        # occasionally "swallow" the intermediate dim frame, so the blink
        # read as a jump rather than a smooth pulse. 24 Hz comfortably
        # covers the animation step and removes that desync.
        with Live(console=console, refresh_per_second=24, transient=True) as live:
            for _ in range(3):
                live.update(f"[bold {_C_OK}]{text}[/bold {_C_OK}]")
                time.sleep(0.16)
                live.update(f"[{_C_DIM}]{text}[/{_C_DIM}]")
                time.sleep(0.10)
        console.print(f"[bold {_C_OK}]{text}[/bold {_C_OK}]")
    except Exception:
        try:
            console.print(f"[bold {_C_OK}]{text}[/bold {_C_OK}]")
        except Exception:
            pass


def check_environment(require_camera: bool = False) -> bool:
    """Checks that the required binaries are present. Returns True if all is well.

    Previously the check was instant and silent — the user couldn't see that
    anything was being checked at all (especially noticeable on the first
    run, when check_environment is called before the intro banner). A short
    status spinner was added: the binary lookup itself is fast, but feedback
    that "a check is running" matters more than speed — otherwise the screen
    just goes silent for a fraction of a second, which reads as a freeze
    rather than "all good, moving on".
    """
    binaries_to_check = dict(REQUIRED_BINARIES)
    if require_camera:
        binaries_to_check["termux-camera-photo"] = OPTIONAL_BINARIES["termux-camera-photo"]

    with console.status(f"[{_C_INFO}]Checking environment...[/{_C_INFO}]", spinner="dots"):
        missing = [
            (binary, hint) for binary, hint in binaries_to_check.items()
            if shutil.which(binary) is None
        ]
        time.sleep(0.15)  # without this the spinner sometimes doesn't have time to render on a fast disk

    if missing:
        console.print()
        for binary, hint in missing:
            _err(f"'{binary}' not found. Install it: {hint}")
        console.print()
        return False

    return True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    output_dir: str = str(DEFAULT_OUTPUT_DIR)
    chunk_size: int = DEFAULT_CHUNK_SIZE
    error_correction: str = "M"  # L / M / Q / H (error correction level)

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                return cls(
                    output_dir=data.get("output_dir", str(DEFAULT_OUTPUT_DIR)),
                    chunk_size=data.get("chunk_size", DEFAULT_CHUNK_SIZE),
                    error_correction=data.get("error_correction", "M"),
                )
            except (json.JSONDecodeError, OSError):
                _warn("Could not read the config, using default values")
        return cls()

    def save(self):
        data = {
            "output_dir": self.output_dir,
            "chunk_size": self.chunk_size,
            "error_correction": self.error_correction,
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            _err(f"Could not save the config: {e}")


# ---------------------------------------------------------------------------
# QR version estimation (for preventive warnings)
# ---------------------------------------------------------------------------

# Approximate capacity (bytes, byte-mode) for error correction levels by QR version.
# This is a rough table, good enough for UX warnings, not for exact calculations.
_QR_CAPACITY_BYTE_M = [
    0, 14, 26, 42, 62, 84, 106, 122, 152, 180, 213,  # versions 0-10
    251, 287, 331, 362, 412, 450, 504, 560, 624, 666,  # 11-20
    711, 779, 857, 911, 997, 1059, 1125, 1190, 1264, 1370,  # 21-30
    1452, 1538, 1628, 1722, 1809, 1911, 1989, 2099, 2213, 2331,  # 31-40
]


def estimate_qr_version(payload_len: int, level: str = "M") -> int:
    """Rough estimate of the QR version needed for payload_len bytes."""
    # capacity is higher for L, lower for Q/H; use the M table with a correction factor
    factor = {"L": 1.27, "M": 1.0, "Q": 0.72, "H": 0.55}.get(level, 1.0)
    for version, cap in enumerate(_QR_CAPACITY_BYTE_M):
        if payload_len <= cap * factor:
            return version
    return 40  # beyond the maximum


def chunk_payload_len(raw_chunk_size: int) -> int:
    """Estimates the final QR payload length for a given raw chunk size."""
    b64_len = int(raw_chunk_size * 4 / 3) + 4
    # + service fields QRVAULT|index|total|hash|
    overhead = len(QR_PREFIX) + 1 + 6 + 1 + 6 + 1 + 8 + 1
    return b64_len + overhead


def warn_if_chunk_too_big(raw_chunk_size: int, level: str) -> bool:
    """Returns True if the chunk size is within the practical printing ceiling."""
    payload_len = chunk_payload_len(raw_chunk_size)
    version = estimate_qr_version(payload_len, level)
    if raw_chunk_size > PRACTICAL_MAX_CHUNK_SIZE or version > 30:
        _warn(
            f"A chunk of {raw_chunk_size} bytes will produce a QR code of roughly version {version} "
            f"(level {level}). Such a QR code may scan poorly with a camera when printed at 5×5 cm "
            f"(the modules are too small). A size up to {PRACTICAL_MAX_CHUNK_SIZE} bytes is recommended."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Crypto: openssl wrappers
# ---------------------------------------------------------------------------

class CryptoError(Exception):
    pass


def encrypt_file(input_path: Path, output_path: Path, password: str):
    """Encrypts a file via openssl AES-256-CBC + pbkdf2. The password is passed via stdin."""
    cmd = [
        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
        "-in", str(input_path),
        "-out", str(output_path),
        "-pass", "stdin",
    ]
    try:
        result = subprocess.run(
            cmd,
            # openssl's BIO password reader expects a line terminated by \n —
            # empty stdin (empty password, no newline) makes some openssl
            # builds report "Error reading password from BIO" instead of
            # accepting an empty password. Always send the trailing newline.
            input=password.encode("utf-8") + b"\n",
            capture_output=True,
            timeout=60,
        )
    finally:
        password = None  # noqa: F841 — attempt to drop the reference to the password
    if result.returncode != 0:
        raise CryptoError(result.stderr.decode("utf-8", errors="replace").strip() or "Unknown openssl error")


def decrypt_file(input_path: Path, output_path: Path, password: str):
    """Decrypts a file via openssl. The password is passed via stdin."""
    cmd = [
        "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
        "-in", str(input_path),
        "-out", str(output_path),
        "-pass", "stdin",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=password.encode("utf-8") + b"\n",
            capture_output=True,
            timeout=60,
        )
    finally:
        password = None  # noqa: F841
    if result.returncode != 0:
        raise CryptoError("Incorrect password or corrupted data")


def sha256_short(path: Path) -> str:
    """First 8 characters of the file's SHA256."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()[:8]


# ---------------------------------------------------------------------------
# QR chunk generation
# ---------------------------------------------------------------------------

def generate_qr_chunks(encrypted_path: Path, filehash: str, chunk_size: int,
                        output_dir: Path, error_correction: str = "M") -> list[Path]:
    """
    Base64-encodes the encrypted file, splits it into chunks, and generates
    a QR PNG in output_dir for each chunk. Returns a list of PNG paths.
    """
    data = encrypted_path.read_bytes()
    b64_data = base64.b64encode(data).decode("ascii")

    chunks = [b64_data[i:i + chunk_size] for i in range(0, len(b64_data), chunk_size)] or [""]
    total = len(chunks)

    output_dir.mkdir(parents=True, exist_ok=True)
    png_paths = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Generating QR codes...", total=total)
        for idx, chunk in enumerate(chunks, start=1):
            payload = f"{QR_PREFIX}|{idx}|{total}|{filehash}|{chunk}"
            png_path = output_dir / f"chunk_{idx:04d}_of_{total:04d}.png"
            cmd = [
                "qrencode", "-l", error_correction, "-o", str(png_path), payload
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                raise CryptoError(
                    f"qrencode failed to generate a QR code for chunk {idx}/{total} "
                    f"(the chunk may be too large): {stderr}"
                )
            png_paths.append(png_path)
            progress.update(task, advance=1)

    return png_paths


# ---------------------------------------------------------------------------
# Reading QR codes
# ---------------------------------------------------------------------------

@dataclass
class QRChunk:
    index: int
    total: int
    filehash: str
    data: str


def parse_qr_payload(raw: str) -> QRChunk | None:
    """Parses a string in the format QRVAULT|index|total|filehash|base64_chunk."""
    parts = raw.split("|", 4)
    if len(parts) != 5 or parts[0] != QR_PREFIX:
        return None
    try:
        index = int(parts[1])
        total = int(parts[2])
    except ValueError:
        return None
    filehash = parts[3]
    data = parts[4]
    return QRChunk(index=index, total=total, filehash=filehash, data=data)


def read_qr_from_file(png_path: Path) -> QRChunk | None:
    """Runs zbarimg on a PNG file and parses the result."""
    result = subprocess.run(
        ["zbarimg", "-q", "--raw", str(png_path)],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    return parse_qr_payload(raw)


def read_any_qr_from_file(image_path: Path) -> tuple[list[str], str]:
    """Reads ALL QR codes from an image (not just the QRVAULT format) —
    regular links, text, custom/colored QR codes (e.g. Telegram).
    zbarimg recognizes codes by module contrast and doesn't depend on color.

    Returns (list_of_raw_strings, status). The list may contain several
    values if the image has more than one QR code.
    """
    result = subprocess.run(
        ["zbarimg", "-q", "--raw", str(image_path)],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        if result.returncode == 4:
            return [], "no_qr_found"
        err = result.stderr.decode("utf-8", errors="replace").strip()
        return [], f"zbar_fail: {err or 'unknown'}"
    raw_out = result.stdout.decode("utf-8", errors="replace").strip()
    if not raw_out:
        return [], "no_qr_found"
    # zbarimg separates multiple found codes with a newline character
    values = [line for line in raw_out.split("\n") if line]
    return values, "ok"


def _parse_zbar_xml(xml_text: str) -> tuple[str | None, tuple[int, int, int, int] | None]:
    """Parses zbarimg -Sxml output and extracts the data and bounding box of the QR code.

    Different zbar versions return the polygon differently:
      - old format: separate <point x="123" y="456"/> tags
      - new format (zbar >= 0.23): a single polygon="+123,+456 +789,+10"
        attribute inside <symbol ...>, with signed coordinates (+/-).
    Both are supported, otherwise on some devices bbox would always be None.

    Returns (raw_data, (x_min, y_min, x_max, y_max)) or (None, None) if
    parsing failed (not treated as an error — there just won't be a box).
    """
    import re
    # <data><![CDATA[...]]></data>
    data_match = re.search(r"<data>\s*<!\[CDATA\[(.*?)\]\]>\s*</data>", xml_text, re.DOTALL)
    raw = data_match.group(1) if data_match else None

    bbox = None

    # New format: <polygon points='+698,+1956 +673,+2388 +1098,+2385 +1109,+1958'/>
    poly_match = re.search(r'<polygon\s+points=[\'"]([^\'"]+)[\'"]', xml_text)
    if poly_match:
        coord_pairs = re.findall(r'([+-]?\d+),([+-]?\d+)', poly_match.group(1))
        if coord_pairs:
            xs = [int(x) for x, _ in coord_pairs]
            ys = [int(y) for _, y in coord_pairs]
            bbox = (min(xs), min(ys), max(xs), max(ys))

    # Old format: <point x="123" y="456"/> — usually 4 polygon points around the QR code
    if bbox is None:
        points = re.findall(r'<point\s+x="(-?\d+)"\s+y="(-?\d+)"\s*/>', xml_text)
        if points:
            xs = [int(x) for x, _ in points]
            ys = [int(y) for _, y in points]
            bbox = (min(xs), min(ys), max(xs), max(ys))

    return raw, bbox


def _parse_zbar_xml_multi(xml_text: str) -> list[tuple[str, tuple[int, int, int, int] | None]]:
    """Parses zbarimg -Sxml output and extracts ALL QR codes found in the
    frame (not just the first, like _parse_zbar_xml) — needed for multi-QR
    capture (one shot with several codes in frame).

    Returns a list of (raw_data, bbox_or_None) — one entry per recognized
    <symbol> in the XML. The list order matches the XML order (usually
    zbar's detection order, NOT guaranteed to be meaningful) — calling code
    must not rely on this order to assemble the file, only on chunk.index
    from the data itself.
    """
    import re
    results = []
    # Each <symbol ...>...</symbol> contains one found code
    for sym_match in re.finditer(r"<symbol\b[^>]*>.*?</symbol>", xml_text, re.DOTALL):
        sym_xml = sym_match.group(0)
        data_match = re.search(r"<data>\s*<!\[CDATA\[(.*?)\]\]>\s*</data>", sym_xml, re.DOTALL)
        if not data_match:
            continue
        raw = data_match.group(1)

        bbox = None
        poly_match = re.search(r'<polygon\s+points=[\'"]([^\'"]+)[\'"]', sym_xml)
        if poly_match:
            coord_pairs = re.findall(r'([+-]?\d+),([+-]?\d+)', poly_match.group(1))
            if coord_pairs:
                xs = [int(x) for x, _ in coord_pairs]
                ys = [int(y) for _, y in coord_pairs]
                bbox = (min(xs), min(ys), max(xs), max(ys))
        if bbox is None:
            points = re.findall(r'<point\s+x="(-?\d+)"\s+y="(-?\d+)"\s*/>', sym_xml)
            if points:
                xs = [int(x) for x, _ in points]
                ys = [int(y) for _, y in points]
                bbox = (min(xs), min(ys), max(xs), max(ys))

        results.append((raw, bbox))
    return results


def render_ascii_frame(image_path: Path, width: int = 60,
                        bbox: tuple[int, int, int, int] | None = None,
                        found: bool = False,
                        boxes: list[tuple[tuple[int, int, int, int], bool]] | None = None) -> str:
    """Renders a JPEG as colored ASCII art (RGB → truecolor rich markup).

    Single-box mode (backward compatible, Modes 3/5 single capture):
    pass bbox — draws a box around the found QR code (green if found=True,
    yellow if merely visible).

    Multi-box mode (key-press multi-QR capture): pass boxes — a list of
    (bbox, found) pairs, each drawn in its own color independently.
    If boxes is given, the bbox/found parameters are ignored.

    Without Pillow, returns a placeholder with a text notice — the box by
    coordinates won't be drawn without pixel data anyway, but the status
    stays informative instead of crashing.
    """
    if not PIL_AVAILABLE:
        return "[dim](Pillow is needed for the ASCII preview: pip install pillow)[/dim]"

    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        return f"[red](failed to open the frame: {e})[/red]"

    orig_w, orig_h = img.size
    # Terminal characters are roughly 2x taller than wide — halve the height
    aspect_correction = 0.5
    height = max(1, int(width * (orig_h / orig_w) * aspect_correction))
    small = img.resize((width, height))
    pixels = small.load()

    ramp = " .:-=+*#%@"
    scale_x = orig_w / width
    scale_y = orig_h / height

    # Normalize into a single list of boxes to draw, regardless of which
    # call mode was used.
    box_list: list[tuple[tuple[int, int, int, int], bool]] = []
    if boxes:
        box_list = [b for b in boxes if b[0]]
    elif bbox:
        box_list = [(bbox, found)]

    lines = []
    for row in range(height):
        line_parts = []
        for col in range(width):
            r, g, b = pixels[col, row]
            brightness = (r * 299 + g * 587 + b * 114) // 1000  # luma
            char = ramp[min(len(ramp) - 1, brightness * len(ramp) // 256)]

            # If several boxes overlap the same ASCII cell, "found" (green)
            # takes priority, so an accepted chunk stands out over an
            # unaccepted one when they overlap.
            border_found = None
            for box_bbox, box_found in box_list:
                x_min, y_min, x_max, y_max = box_bbox
                px, py = col * scale_x, row * scale_y
                margin_x, margin_y = scale_x, scale_y
                near_left_right = abs(px - x_min) < margin_x or abs(px - x_max) < margin_x
                near_top_bottom = abs(py - y_min) < margin_y or abs(py - y_max) < margin_y
                within_x = x_min - margin_x <= px <= x_max + margin_x
                within_y = y_min - margin_y <= py <= y_max + margin_y
                if (near_left_right and within_y) or (near_top_bottom and within_x):
                    if box_found:
                        border_found = True
                        break
                    elif border_found is None:
                        border_found = False

            if border_found is not None:
                color = "bold bright_green" if border_found else "bold yellow"
                line_parts.append(f"[{color}]#[/{color}]")
            else:
                # The real pixel color via rich truecolor markup
                line_parts.append(f"[rgb({r},{g},{b})]{char}[/rgb({r},{g},{b})]")
        lines.append("".join(line_parts))
    return "\n".join(lines)


def _capture_frame_for_scan(camera_id: str = "0") -> tuple[Path | None, Path | None, float, str]:
    """Captures a frame with the camera and prepares an enhanced copy for
    zbar (upscale + autocontrast + sharpen). Shared by single and multi-QR capture.

    Returns (snapshot_path_or_None, scan_target_path_or_None,
    scale_factor, status). On error snapshot/scan_target are None, and the
    status explains why ("camera_fail: ...", "ok").
    """
    snapshot = TEMP_DIR / f"file_to_qr_scan_{os.getpid()}.jpg"
    _t0 = time.monotonic()
    result = subprocess.run(
        ["termux-camera-photo", "-c", camera_id, str(snapshot)],
        capture_output=True,
        timeout=15,
    )
    _t_camera = time.monotonic() - _t0
    if result.returncode != 0 or not snapshot.exists():
        err = result.stderr.decode("utf-8", errors="replace").strip()
        return None, None, 1.0, f"camera_fail: {err or 'no output file'}"
    if snapshot.stat().st_size == 0:
        return None, None, 1.0, "camera_fail: empty photo file"

    scan_target = snapshot
    scale_factor = 1.0
    _t1 = time.monotonic()
    if PIL_AVAILABLE:
        try:
            enhanced_path = TEMP_DIR / f"file_to_qr_scan_enhanced_{os.getpid()}.png"
            img = Image.open(snapshot).convert("L")
            if max(img.size) < 1600:
                factor = 1600 / max(img.size)
                img = img.resize(
                    (int(img.width * factor), int(img.height * factor)),
                    Image.LANCZOS,
                )
                scale_factor = factor
            img = ImageOps.autocontrast(img, cutoff=1)
            img = img.filter(ImageFilter.SHARPEN)
            img.save(enhanced_path)
            scan_target = enhanced_path
        except Exception:
            scan_target = snapshot
            scale_factor = 1.0
    _t_enhance = time.monotonic() - _t1
    if FILE_TO_QR_DEBUG_TIMING:
        sys.stderr.write(f"[timing] camera={_t_camera:.2f}s enhance={_t_enhance:.2f}s\n")

    return snapshot, scan_target, scale_factor, "ok"


def _capture_and_scan_qr(camera_id: str = "0") -> tuple[str | None, str, Path | None, tuple | None]:
    """Captures a frame with the camera, enhances it, and tries to recognize
    ANY QR code (not just the QRVAULT format). Returns (raw_text_or_None,
    status, path_to_frame_or_None, bbox_or_None).

    This is the shared low-level function for two modes: assembling a file
    from QR codes (Mode 3, where raw is parsed as QRVAULT|...) and the
    generic QR scanner (Mode 5, where raw is just the code's content as-is:
    a link, text, a Telegram login, etc., including colored/custom QR codes —
    zbarimg recognizes codes by module contrast and doesn't depend on color).
    """
    try:
        snapshot, scan_target, scale_factor, status = _capture_frame_for_scan(camera_id)
        if snapshot is None:
            return None, status, None, None

        _t2 = time.monotonic()
        zresult = subprocess.run(
            ["zbarimg", "-q", "--xml", "--polygon", str(scan_target)],
            capture_output=True,
            timeout=15,
        )
        _t_zbar = time.monotonic() - _t2
        if FILE_TO_QR_DEBUG_TIMING:
            sys.stderr.write(f"[timing] zbar={_t_zbar:.2f}s\n")
        if scan_target != snapshot:
            try:
                scan_target.unlink()
            except OSError:
                pass
        if zresult.returncode != 0:
            # zbarimg returncode 4 = no QR found in the frame (this is normal, not an error)
            if zresult.returncode == 4:
                return None, "no_qr_found", snapshot, None
            err = zresult.stderr.decode("utf-8", errors="replace").strip()
            return None, f"zbar_fail: {err or 'unknown'}", snapshot, None

        xml_out = zresult.stdout.decode("utf-8", errors="replace")
        raw, bbox = _parse_zbar_xml(xml_out)
        if not raw:
            return None, "no_qr_found", snapshot, None
        # bbox arrived in the scan_target coordinate system (which may be an
        # upscaled version of snapshot) — convert it back to snapshot
        # coordinates, on which the ASCII preview is built, otherwise the box
        # is drawn in the wrong place.
        if bbox and scale_factor != 1.0:
            x_min, y_min, x_max, y_max = bbox
            bbox = (
                int(x_min / scale_factor),
                int(y_min / scale_factor),
                int(x_max / scale_factor),
                int(y_max / scale_factor),
            )
        return raw, "ok", snapshot, bbox
    except subprocess.TimeoutExpired:
        return None, "timeout", None, None


def read_any_qr_from_camera(camera_id: str = "0") -> tuple[str | None, str, Path | None, tuple | None]:
    """Captures a frame and recognizes ANY QR code (not just QRVAULT) — a
    regular link, text, an account login, etc. A thin wrapper over
    _capture_and_scan_qr with no parsing for the vault format."""
    return _capture_and_scan_qr(camera_id)


def capture_and_scan_multi_qr(camera_id: str = "0") -> tuple[list[tuple[str, tuple | None]], str, Path | None]:
    """Captures ONE frame with the camera and recognizes ALL QR codes in it
    at once (e.g. a sheet with 5-8 printed codes). Used in the key-press
    capture mode, where the user decides when to shoot, rather than in a
    continuous auto-loop.

    Returns (a list of (raw_data, bbox_or_None) for each found code,
    status, path_to_frame_or_None). The list order is zbar's detection
    order in the frame, NOT guaranteed to be the index order in the data;
    file assembly always goes by chunk.index, not by this order.
    """
    try:
        snapshot, scan_target, scale_factor, status = _capture_frame_for_scan(camera_id)
        if snapshot is None:
            return [], status, None

        zresult = subprocess.run(
            ["zbarimg", "-q", "--xml", "--polygon", str(scan_target)],
            capture_output=True,
            timeout=15,
        )
        if scan_target != snapshot:
            try:
                scan_target.unlink()
            except OSError:
                pass
        if zresult.returncode != 0:
            if zresult.returncode == 4:
                return [], "no_qr_found", snapshot
            err = zresult.stderr.decode("utf-8", errors="replace").strip()
            return [], f"zbar_fail: {err or 'unknown'}", snapshot

        xml_out = zresult.stdout.decode("utf-8", errors="replace")
        found = _parse_zbar_xml_multi(xml_out)
        if not found:
            return [], "no_qr_found", snapshot

        # Convert each bbox back to snapshot coordinates (see the comment in
        # _capture_and_scan_qr about scan_target vs snapshot).
        results = []
        for raw, bbox in found:
            if bbox and scale_factor != 1.0:
                x_min, y_min, x_max, y_max = bbox
                bbox = (
                    int(x_min / scale_factor),
                    int(y_min / scale_factor),
                    int(x_max / scale_factor),
                    int(y_max / scale_factor),
                )
            results.append((raw, bbox))
        return results, "ok", snapshot
    except subprocess.TimeoutExpired:
        return [], "timeout", None


def read_multi_qrvault_from_camera(camera_id: str = "0") -> tuple[list[tuple[QRChunk | None, str]], list[tuple], str, Path | None]:
    """Captures one frame and parses all found QR codes as QRVAULT chunks
    (Mode 3, multi-capture). Chunks that don't parse as the QRVAULT format
    (e.g. a stray QR code that ended up in the frame) are returned as
    (None, "parse_fail: ...") — the calling code decides whether to show a
    warning, but this doesn't stop processing of the other codes in the frame.

    Returns (a list of (chunk_or_None, status) for each found code,
    a list of bboxes in the SAME order — used for highlighting on the
    preview, the overall frame status, the path to the snapshot).
    """
    found, status, snapshot = capture_and_scan_multi_qr(camera_id)
    if not found:
        return [], [], status, snapshot
    parsed = []
    bboxes = []
    for raw, bbox in found:
        bboxes.append(bbox)
        try:
            chunk = parse_qr_payload(raw)
            if chunk is None:
                parsed.append((None, "not_qrvault_format"))
            else:
                parsed.append((chunk, "ok"))
        except Exception as e:
            parsed.append((None, f"parse_fail: {e}"))
    return parsed, bboxes, "ok", snapshot


# ---------------------------------------------------------------------------
# Chunk assembly
# ---------------------------------------------------------------------------

@dataclass
class AssemblyState:
    chunks: dict = field(default_factory=dict)  # index -> QRChunk
    total: int | None = None
    filehash: str | None = None

    def add(self, chunk: QRChunk) -> tuple[bool, str]:
        """Adds a chunk. Returns (success, message)."""
        if self.total is None:
            if chunk.total < 1:
                return False, f"Invalid total in the QR code: {chunk.total}"
            self.total = chunk.total
            self.filehash = chunk.filehash
        else:
            if chunk.total != self.total:
                return False, f"Chunk {chunk.index} has a different total chunk count ({chunk.total} vs {self.total})"
            if chunk.filehash != self.filehash:
                return False, f"Chunk {chunk.index} belongs to a different file (hash {chunk.filehash} vs {self.filehash})"
        # The index must fall within 1..total — otherwise a corrupted/foreign
        # scan could silently get stuck in the dict under a nonexistent
        # number, and is_complete() would never become True (or worse, junk
        # would get assembled that then mysteriously fails to decrypt).
        if not (1 <= chunk.index <= self.total):
            return False, f"Chunk with invalid index {chunk.index} (expected 1..{self.total})"
        if chunk.index in self.chunks:
            if self.chunks[chunk.index].data != chunk.data:
                return False, f"Chunk {chunk.index} already exists but with different data — possibly a corrupted scan"
            return False, "duplicate"
        self.chunks[chunk.index] = chunk
        return True, "ok"

    def missing_indices(self) -> list[int]:
        if self.total is None:
            return []
        return [i for i in range(1, self.total + 1) if i not in self.chunks]

    def is_complete(self) -> bool:
        return self.total is not None and not self.missing_indices()

    def assembled_base64(self) -> str:
        return "".join(self.chunks[i].data for i in range(1, self.total + 1))


def assemble_chunks(chunks: list[QRChunk]) -> AssemblyState:
    """Assembles a list of QRChunk into an AssemblyState with validation."""
    state = AssemblyState()
    errors = []
    for chunk in chunks:
        ok, msg = state.add(chunk)
        if not ok and msg != "duplicate":
            errors.append(msg)
    if errors:
        for e in errors:
            console.print(f"[{_C_ERR}]⚠ {e}[/{_C_ERR}]")
    return state


def build_decrypted_source_from_state(state: AssemblyState, dest_encrypted: Path) -> bool:
    """Decodes base64 from the assembled state, writes the encrypted file, verifies the hash."""
    b64_data = state.assembled_base64()
    try:
        raw = base64.b64decode(b64_data)
    except Exception as e:
        _err(f"Base64 decoding error: {e}")
        return False
    dest_encrypted.write_bytes(raw)
    actual_hash = sha256_short(dest_encrypted)
    if actual_hash != state.filehash:
        console.print(
            f"[{_C_ERR}]⚠ The hash of the assembled file ({actual_hash}) does not match the expected one ({state.filehash}). "
            f"The data may be corrupted.[/{_C_ERR}]"
        )
        return Confirm.ask(f"[{_C_WARN}]Continue decrypting despite the hash mismatch?[/{_C_WARN}]", default=False)
    return True


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

# Large block ASCII font, 5 rows tall — custom, hand-made (not a QR-like
# pattern, to avoid confusion with real QR codes on screen).
# Each letter is a list of 5 rows of 5-6 characters.
_BLOCK_FONT = {
    "U": ["██  ██", "██  ██", "██  ██", "██  ██", "██  ██", "██  ██", " ████ "],
    "D": ["█████ ", "██  ██", "██  ██", "██  ██", "██  ██", "██  ██", "█████ "],
    "N": ["██  ██", "███ ██", "██ ███", "██  ██", "██  ██", "██  ██", "██  ██"],
    "M": ["██  ██", "██████", "██████", "██  ██", "██  ██", "██  ██", "██  ██"],
    "P": ["██████", "██  ██", "██  ██", "██████", "██    ", "██    ", "██    "],
    "A": [" ████ ", "██  ██", "██  ██", "██████", "██  ██", "██  ██", "██  ██"],
    " ": ["      ", "      ", "      ", "      ", "      ", "      ", "      "],
}
_FONT_ROWS = 7

_BANNER_TEXT = "UDNMPA"

# Palette for the shimmering per-letter gradient — cycle through these
# colors frame by frame, creating a "running highlight" effect on the text.
_BANNER_PALETTE = [
    "bright_green", "green", "cyan", "bright_cyan", "bright_blue", "blue",
    "bright_magenta", "magenta", "bright_green",
]


def _render_block_text(text: str, letter_colors: list[str] | None = None) -> str:
    """Builds a block-ASCII render of the string text, _FONT_ROWS rows tall.
    letter_colors, if given, colors each letter individually (for a
    gradient/pulse effect) — its length must match the length of text; if
    None, the whole text uses one color."""
    letters = [_BLOCK_FONT.get(ch.upper(), _BLOCK_FONT[" "]) for ch in text]
    lines = []
    for row in range(_FONT_ROWS):
        parts = []
        for i, letter in enumerate(letters):
            glyph_row = letter[row]
            if letter_colors:
                color = letter_colors[i]
                parts.append(f"[bold {color}]{glyph_row}[/bold {color}]")
            else:
                parts.append(glyph_row)
            parts.append(" ")  # gap between letters
        lines.append("".join(parts))
    return "\n".join(lines)


def show_intro_animation():
    """Animated splash screen on startup — the large UDNMPA text:

      Phase 1: letters appear one by one left to right (column by column),
               slowly enough for the effect to be noticeable, not just a flash.
      Phase 2: a few seconds of "running" color highlighting across the
               letters — a shimmering gradient shifting through the palette
               frame by frame.
      Phase 3: a tagline is typed character by character under the text.

    A fully decorative layer on top of Live() — wrapped best-effort: if the
    terminal can't handle the animation, it just prints the text statically
    and moves on, without breaking anything else in the program.
    """
    try:
        # refresh_per_second raised from 20 to 30: phase 2's step (0.09s
        # between frames) requires Live to redraw faster than once every
        # 50ms, otherwise on slow Termux terminals the gradient frames
        # sometimes collapse and the "run" across letters reads as jerky
        # rather than a smooth wave. 30 Hz gives headroom without a
        # noticeable CPU load on the phone.
        with Live(console=console, refresh_per_second=30, transient=True) as live:
            # Phase 1: letters appear one after another left to right
            for n in range(1, len(_BANNER_TEXT) + 1):
                partial = _BANNER_TEXT[:n] + " " * (len(_BANNER_TEXT) - n)
                rendered = _render_block_text(partial, [_C_OK] * len(_BANNER_TEXT))
                live.update(Panel(rendered, expand=False, border_style="green"))
                time.sleep(0.18)

            # Phase 2: a shimmering gradient "runs" across the letters for a few cycles
            palette_len = len(_BANNER_PALETTE)
            cycles = 2
            total_shifts = palette_len * cycles
            for shift in range(total_shifts):
                colors = [
                    _BANNER_PALETTE[(i + shift) % palette_len]
                    for i in range(len(_BANNER_TEXT))
                ]
                rendered = _render_block_text(_BANNER_TEXT, colors)
                live.update(Panel(rendered, expand=False, border_style=_C_OK))
                time.sleep(0.09)

            # Final clean frame before exiting Live
            final = _render_block_text(_BANNER_TEXT, [_C_OK] * len(_BANNER_TEXT))
            live.update(Panel(final, expand=False, border_style=_C_OK))
            time.sleep(0.4)

        # Live(transient=True) clears the panel on exit — print the final
        # frame once more statically so the text stays on screen.
        final = _render_block_text(_BANNER_TEXT, [_C_OK] * len(_BANNER_TEXT))
        console.print(Panel(final, expand=False, border_style=_C_OK))
        console.print()
    except Exception:
        # The animation is not a critical path. If something in the
        # terminal isn't supported, just print the text statically and move on.
        try:
            console.print(Panel(_render_block_text(_BANNER_TEXT), expand=False, border_style="green"))
            console.print()
        except Exception:
            pass


# Menu items: (number, icon, text, color, description). Color is tied to
# the item's meaning and chosen to match the UDNMPA banner palette.
_MENU_ITEMS = [
    ("1", "🔐", "Encrypt file → QR", "bright_green", "source → cipher → QR set"),
    ("2", "📂", "Assemble file from QR (from folder)", "cyan", "PNG files → decryption"),
    ("3", "📷", "Assemble file from QR (camera)", "bright_cyan", "capture on Enter, any order"),
    ("4", "⚙ ", "Settings", "yellow", "chunk size, error correction"),
    ("5", "🔎", "Scan any QR", "bright_magenta", "links, text — no decryption"),
    ("0", "🚪", "Exit", "grey70", ""),
]

_MENU_DIVIDER = "[bright_black]" + "─" * 44 + "[/bright_black]"


def _menu_row(num: str, icon: str, text: str, color: str, desc: str, cursor: bool = False) -> str:
    """One menu item row + a muted description caption below it."""
    marker = f"[bold {color}]▸[/bold {color}]" if cursor else " "
    head = f"{marker} [bold {color}]{icon}  {num}[/bold {color}]  [{color}]{text}[/{color}]"
    if not desc:
        return head
    return f"{head}\n     [bright_black]{desc}[/bright_black]"


def _menu_panel(rows: list[str], footer: str | None = None) -> Panel:
    body = "\n".join(rows) if rows else " "
    if footer:
        body += f"\n{_MENU_DIVIDER}\n{footer}"
    return Panel(
        body,
        title="[bold bright_green]◆ FILE TO QR[/bold bright_green] [bright_black]v1.0 · offline[/bright_black]",
        subtitle="[bright_black]cold storage[/bright_black]",
        border_style="bright_green",
        expand=False,
        padding=(1, 2),
    )


def _menu_footer() -> str:
    return "[bright_black]Choose a number + Enter[/bright_black]"


def show_main_menu(animate: bool = False):
    """Prints the main menu. When animate=True, items appear line by line
    via Live, with a running ▸ cursor on the current line (only on the first
    display of the session — in the main() loop on subsequent visits
    animate=False is used, so the animation doesn't get tiresome on every
    return from a sub-mode)."""
    footer = _menu_footer()
    full_rows = [_menu_row(*item) for item in _MENU_ITEMS]

    if not animate:
        console.print(_menu_panel(full_rows, footer))
        return

    try:
        with Live(console=console, refresh_per_second=30, transient=True) as live:
            shown: list[str] = []
            for item in _MENU_ITEMS:
                # Frame 1: the line appears with a ▸ cursor (emphasizing "typing now")
                shown.append(_menu_row(*item, cursor=True))
                live.update(_menu_panel(shown))
                time.sleep(0.075)
                # Frame 2: the cursor fades, the line settles into its normal look
                shown[-1] = _menu_row(*item)
                live.update(_menu_panel(shown))
                time.sleep(0.035)
            live.update(_menu_panel(shown, footer))
            time.sleep(0.2)
        console.print(_menu_panel(full_rows, footer))
    except Exception:
        # The animation isn't critical — on any failure, just print it statically.
        console.print(_menu_panel(full_rows, footer))


# ---------------------------------------------------------------------------
# Mode 1: Encrypt file → QR
# ---------------------------------------------------------------------------

def menu_encrypt(config: Config):
    console.print(_section_panel("🔐 Encrypt file → QR"))

    default_path = str(DEFAULT_DOWNLOADS) if DEFAULT_DOWNLOADS.exists() else str(Path.home())
    file_str = Prompt.ask("Path to the file to encrypt", default=default_path)
    input_path = Path(file_str).expanduser()

    if not input_path.exists() or not input_path.is_file():
        _err(f"File not found: {input_path}")
        return

    size = input_path.stat().st_size
    if size > MAX_INPUT_FILE_BYTES:
        _err(
            f"File too large: {size} bytes (limit {MAX_INPUT_FILE_BYTES} bytes / 52 KB). "
            f"Shrink the archive or split it."
        )
        return
    _ok(f"File found: {input_path} ({size} bytes)")

    # Password
    while True:
        password = getpass("Password: ")
        password2 = getpass("Repeat password: ")
        if password != password2:
            _warn("Passwords don't match, try again")
            continue
        if password == "":
            if not Confirm.ask("[yellow]The password is empty. Really go without a password?[/yellow]", default=False):
                continue
        break

    # Chunk size
    chunk_size = config.chunk_size
    if Confirm.ask(f"Use the default chunk size ({chunk_size} bytes)?", default=True):
        pass
    else:
        chunk_size = int(Prompt.ask("Chunk size (bytes)", default=str(chunk_size)))
    warn_if_chunk_too_big(chunk_size, config.error_correction)

    tmp_encrypted = TEMP_DIR / f"file_to_qr_{os.getpid()}.enc"
    try:
        status = console.status(f"[bold {_C_OK}]🔐 Encrypting the file...[/bold {_C_OK}]", spinner="bouncingBar")
        with status:
            try:
                encrypt_file(input_path, tmp_encrypted, password)
            except CryptoError as e:
                _err(f"Encryption error: {e}")
                return
            finally:
                del password
                del password2

            # Hashing the encrypted data — for small files (52 KB limit) this
            # takes a fraction of a second, but without an explicit status it
            # looks like a stalled second between "Encrypting..." and the
            # results table. Updating the text of the same status instead of
            # opening a second Live at the same time (rich doesn't allow
            # concurrent Live displays on one console).
            status.update(f"[{_C_INFO}]Computing hash...[/{_C_INFO}]")
            filehash = sha256_short(tmp_encrypted)

        out_dir_name = f"{input_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}"
        out_dir = Path(config.output_dir).expanduser() / out_dir_name

        try:
            png_paths = generate_qr_chunks(
                tmp_encrypted, filehash, chunk_size, out_dir, config.error_correction
            )
        except CryptoError as e:
            _err(str(e))
            _warn("Try reducing the chunk size and retry.")
            return

    finally:
        try:
            if tmp_encrypted.exists():
                tmp_encrypted.unlink()
        except OSError:
            pass

    table = _styled_table("Encryption result")
    table.add_column("Parameter", style=_C_INFO)
    table.add_column("Value", style=_C_OK)
    table.add_row("Chunks generated", str(len(png_paths)))
    table.add_row("Chunk size", f"{chunk_size} bytes")
    table.add_row("File hash (for verification)", filehash)
    table.add_row("Output folder", str(out_dir))
    console.print(table)

    if Confirm.ask("Show the list of generated files?", default=False):
        for p in png_paths:
            console.print(f"  [{_C_DIM}]{p}[/{_C_DIM}]")


# ---------------------------------------------------------------------------
# Mode 2: Assemble file from QR (from folder)
# ---------------------------------------------------------------------------

def menu_decrypt_folder(config: Config):
    console.print(_section_panel("📂 Assemble file from QR (from folder)"))

    folder_str = Prompt.ask("Path to the folder with PNG QR codes", default=config.output_dir)
    folder = Path(folder_str).expanduser()
    if not folder.exists() or not folder.is_dir():
        _err(f"Folder not found: {folder}")
        return

    png_files = sorted(folder.glob("*.png"))
    if not png_files:
        _err(f"No PNG files in the folder: {folder}")
        return

    chunks: list[QRChunk] = []
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"), console=console,
    ) as progress:
        task = progress.add_task("Scanning QR codes from files...", total=len(png_files))
        for png in png_files:
            chunk = read_qr_from_file(png)
            if chunk is not None:
                chunks.append(chunk)
            progress.update(task, advance=1)

    if not chunks:
        _err("Could not recognize any QR code in the QRVAULT format")
        return

    state = assemble_chunks(chunks)

    table = _styled_table("Assembly status")
    table.add_column("Parameter", style=_C_INFO)
    table.add_column("Value")
    table.add_row("Total chunks expected", str(state.total))
    table.add_row("Found", str(len(state.chunks)))
    missing = state.missing_indices()
    if missing:
        table.add_row("Missing", f"[{_C_ERR}]{', '.join(map(str, missing))}[/{_C_ERR}]")
    else:
        table.add_row("Status", f"[{_C_OK}]All chunks found[/{_C_OK}]")
    console.print(table)

    if missing:
        _err("Not all chunks were found, assembly is not possible")
        return

    _finish_assembly(state, config)


# ---------------------------------------------------------------------------
# Mode 3: Assemble file from QR (camera)
# ---------------------------------------------------------------------------

CAMERA_PROGRESS_FILE = TEMP_DIR / "file_to_qr_camera_progress.json"


def _save_camera_progress(state: AssemblyState):
    try:
        data = {
            "total": state.total,
            "filehash": state.filehash,
            "chunks": {str(i): c.data for i, c in state.chunks.items()},
        }
        CAMERA_PROGRESS_FILE.write_text(json.dumps(data))
    except OSError:
        pass


def _load_camera_progress() -> AssemblyState | None:
    if not CAMERA_PROGRESS_FILE.exists():
        return None
    try:
        data = json.loads(CAMERA_PROGRESS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    state = AssemblyState(total=data["total"], filehash=data["filehash"])
    for idx_str, chunk_data in data["chunks"].items():
        idx = int(idx_str)
        state.chunks[idx] = QRChunk(index=idx, total=state.total, filehash=state.filehash, data=chunk_data)
    return state


def _clear_camera_progress():
    try:
        if CAMERA_PROGRESS_FILE.exists():
            CAMERA_PROGRESS_FILE.unlink()
    except OSError:
        pass


def menu_decrypt_camera(config: Config):
    console.print(_section_panel("📷 Assemble file from QR (camera)"))

    if not check_environment(require_camera=True):
        return

    _info(
        "Capture on Enter press — you decide when to shoot. "
        "You can hold several QR codes in frame at once (e.g. a sheet with 5-8 codes), "
        "the program will find and recognize all of them in a single shot."
    )

    state = None
    if CAMERA_PROGRESS_FILE.exists():
        if Confirm.ask("Unfinished scan progress found. Continue from it?", default=True):
            state = _load_camera_progress()
    if state is None:
        state = AssemblyState()

    camera_id = Prompt.ask("Camera ID (0 = rear, 1 = front)", default="0")

    ascii_width = 60 if PIL_AVAILABLE else 0
    if not PIL_AVAILABLE:
        console.print(
            f"[{_C_DIM}]Tip: install 'pip install pillow' for an ASCII preview of the camera frame.[/{_C_DIM}]"
        )

    consecutive_camera_fails = 0

    try:
        while True:
            if state.total:
                found_n = len(state.chunks)
                missing = state.missing_indices()
                console.print(f"[{_C_OK}]✓ Found {found_n}/{state.total}[/{_C_OK}]" +
                              (f"  [{_C_WARN}]Missing: {', '.join(map(str, missing))}[/{_C_WARN}]" if missing else ""))
            input("Point the camera at the QR code(s) and press Enter to shoot (Ctrl+C to exit)... ")

            parsed, bboxes, status, snapshot_path = read_multi_qrvault_from_camera(camera_id)

            if status.startswith("camera_fail"):
                consecutive_camera_fails += 1
                console.print(f"[red]⚠ The camera isn't responding: {status}[/red]")
                if consecutive_camera_fails >= 3:
                    console.print(
                        f"[yellow]Try a different camera ID (currently: {camera_id}) "
                        f"or check the camera permission for Termux:API.[/yellow]"
                    )
                continue
            consecutive_camera_fails = 0

            if status == "no_qr_found":
                console.print(f"[{_C_DIM}]No QR code found in the frame, try again.[/{_C_DIM}]")
                if snapshot_path:
                    try:
                        snapshot_path.unlink()
                    except OSError:
                        pass
                continue
            if status == "timeout":
                console.print("[red]⚠ The camera didn't respond in time (timeout).[/red]")
                continue

            # For each code found in the frame, decide: accepted (green),
            # already present/duplicate (yellow), or format error/foreign QR
            # (yellow, doesn't block the other codes in the same frame).
            box_list = []
            accepted_count = 0
            for (chunk, parse_status), bbox in zip(parsed, bboxes):
                if chunk is None:
                    if parse_status != "not_qrvault_format":
                        console.print(f"[yellow]⚠ A code in the frame was not recognized: {parse_status}[/yellow]")
                    else:
                        console.print("[yellow]⚠ A code in the frame is not in the QRVAULT format — skipped.[/yellow]")
                    box_list.append((bbox, False))
                    continue
                ok, msg = state.add(chunk)
                if ok:
                    _save_camera_progress(state)
                    accepted_count += 1
                    box_list.append((bbox, True))
                elif msg == "duplicate":
                    box_list.append((bbox, True))  # already accepted earlier — also green
                else:
                    console.print(f"[red]⚠ Chunk {chunk.index}: {msg}[/red]")
                    box_list.append((bbox, False))

            if ascii_width and snapshot_path:
                ascii_frame = render_ascii_frame(snapshot_path, width=ascii_width, boxes=box_list)
                console.print(Panel(ascii_frame, title=f"Snapshot — QR codes found: {len(parsed)}, newly accepted: {accepted_count}"))
            else:
                console.print(f"[{_C_INFO}]QR codes found in frame: {len(parsed)}, newly accepted: {accepted_count}[/{_C_INFO}]")

            if snapshot_path:
                try:
                    snapshot_path.unlink()
                except OSError:
                    pass

            if state.total and state.is_complete():
                break
    except KeyboardInterrupt:
        console.print("\n[yellow]Scanning interrupted by the user.[/yellow]")
        if state.chunks:
            found_n = len(state.chunks)
            total_str = str(state.total) if state.total else "?"
            console.print(f"[{_C_INFO}]Progress saved: {found_n}/{total_str} chunks. Run Mode 3 again to continue.[/{_C_INFO}]")
        return

    _fx_flash_success("✓ All chunks assembled!")
    _clear_camera_progress()
    _finish_assembly(state, config)


# ---------------------------------------------------------------------------
# Shared assembly finale (Modes 2 and 3): decoding + decryption + saving
# ---------------------------------------------------------------------------

def _finish_assembly(state: AssemblyState, config: Config):
    tmp_encrypted = TEMP_DIR / f"file_to_qr_assembled_{os.getpid()}.enc"
    tmp_decrypted = TEMP_DIR / f"file_to_qr_decrypted_{os.getpid()}.out"
    try:
        if not build_decrypted_source_from_state(state, tmp_encrypted):
            _err("Assembly cancelled.")
            return

        password = getpass("Password to decrypt: ")
        try:
            # bright_cyan instead of the old bright_green is intentional: the
            # same semantic code as in sub-mode headers (_C_ACCENT) — this
            # way the "read/restore" operation is visually distinct from
            # "create" (encryption, green) on 🔐, while staying in the same
            # palette as the rest of the UI instead of a random separate color.
            with console.status(f"[bold {_C_ACCENT}]🔓 Decrypting...[/bold {_C_ACCENT}]", spinner="bouncingBar"):
                try:
                    decrypt_file(tmp_encrypted, tmp_decrypted, password)
                except CryptoError as e:
                    _err(str(e))
                    return
        finally:
            del password

        default_out = str(DEFAULT_DOWNLOADS) if DEFAULT_DOWNLOADS.exists() else str(Path.home())
        dest_str = Prompt.ask("Where to save the decrypted file (folder)", default=default_out)
        dest_name = Prompt.ask("File name", default="restored_file")
        dest_path = Path(dest_str).expanduser() / dest_name
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp_decrypted, dest_path)

        _ok(f"File saved: {dest_path}")
        _warn(
            "Warning: this file is stored unencrypted on disk. "
            "Move it to a safe place and don't leave it exposed for long."
        )
    finally:
        for p in (tmp_encrypted, tmp_decrypted):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Mode 5: Scan any QR (links, text, custom/colored QR)
# ---------------------------------------------------------------------------

def _print_scanned_value(value: str):
    """Shows the recognized QR content and, if it looks like a URL,
    suggests convenient actions."""
    console.print(Panel(
        value, title=f"[bold {_C_OK}]QR content[/bold {_C_OK}]",
        border_style=_C_OK, expand=False, padding=(0, 1),
    ))
    looks_like_url = value.strip().lower().startswith(("http://", "https://", "tg://"))
    if looks_like_url:
        console.print(f"[{_C_DIM}]Looks like a link. Copy it and open it in a browser/app.[/{_C_DIM}]")


def menu_scan_any_qr(config: Config):
    """Scans an arbitrary QR code — not just ones created by this script.
    Works for regular links, text, and custom/colored QR codes
    (e.g. special QR codes from Telegram) — zbarimg decodes by module
    contrast and doesn't depend on the code's color."""
    console.print(_section_panel("🔎 Scan any QR"))

    source = Prompt.ask(
        "QR source",
        choices=["camera", "file"],
        default="camera",
    )

    if source == "file":
        path_str = Prompt.ask("Path to the image with the QR code")
        image_path = Path(path_str).expanduser()
        if not image_path.exists():
            _err(f"File not found: {image_path}")
            return
        values, status = read_any_qr_from_file(image_path)
        if status == "no_qr_found":
            _warn("No QR code found in the image.")
            return
        if status != "ok":
            _err(f"Recognition error: {status}")
            return
        for i, value in enumerate(values, 1):
            if len(values) > 1:
                console.print(f"[{_C_DIM}]— QR #{i} —[/{_C_DIM}]")
            _print_scanned_value(value)
        return

    # Source — camera
    if not check_environment(require_camera=True):
        return

    _warn(
        "Note: this isn't true live video, just a capture loop roughly every ~0.3 sec. "
        "Hold the phone steady and watch the lighting."
    )
    camera_id = Prompt.ask("Camera ID (0 = rear, 1 = front)", default="0")
    _info("Scanning... (Ctrl+C to stop)")

    ascii_width = 60 if PIL_AVAILABLE else 0
    if not PIL_AVAILABLE:
        console.print(
            f"[{_C_DIM}]Tip: install 'pip install pillow' for an ASCII preview of the camera frame.[/{_C_DIM}]"
        )

    consecutive_camera_fails = 0
    last_status = "starting"

    try:
        with Live(console=console, refresh_per_second=2) as live:
            while True:
                raw, status, snapshot_path, bbox = read_any_qr_from_camera(camera_id)
                last_status = status
                found_now = raw is not None

                if status.startswith("camera_fail"):
                    consecutive_camera_fails += 1
                    if consecutive_camera_fails >= 3:
                        live.update(Panel(
                            f"[{_C_ERR}]⚠ The camera hasn't responded 3 times in a row: {status}[/{_C_ERR}]\n"
                            f"[{_C_WARN}]Try a different camera ID (currently: {camera_id}) "
                            f"or check the camera permission for Termux:API.[/{_C_WARN}]",
                            title="Scanning QR", border_style=_C_ERR,
                        ))
                        time.sleep(2)
                    else:
                        time.sleep(0.5)
                else:
                    consecutive_camera_fails = 0

                ascii_frame = ""
                if snapshot_path and ascii_width:
                    ascii_frame = render_ascii_frame(
                        snapshot_path, width=ascii_width, bbox=bbox, found=found_now
                    )
                if snapshot_path:
                    try:
                        snapshot_path.unlink()
                    except OSError:
                        pass

                status_line = f"[{_C_DIM}]Looking for a QR code... (last status: {last_status})[/{_C_DIM}]"
                body = f"{ascii_frame}\n\n{status_line}" if ascii_frame else status_line
                live.update(Panel(body, title="Scanning QR", border_style=_C_ACCENT))

                if raw is not None:
                    break

                time.sleep(0.3)
    except KeyboardInterrupt:
        console.print(f"\n[{_C_WARN}]Scanning interrupted by the user.[/{_C_WARN}]")
        return

    _ok("QR code recognized")
    _print_scanned_value(raw)


# ---------------------------------------------------------------------------
# Mode 4: Settings
# ---------------------------------------------------------------------------

def menu_settings(config: Config):
    console.print(_section_panel("⚙ Settings"))

    table = _styled_table("Current settings")
    table.add_column("Parameter", style=_C_INFO)
    table.add_column("Value", style=_C_OK)
    table.add_row("Output folder", config.output_dir)
    table.add_row("Default chunk size", f"{config.chunk_size} bytes")
    table.add_row("Error correction level", config.error_correction)
    console.print(table)

    if not Confirm.ask("Change the settings?", default=False):
        return

    config.output_dir = Prompt.ask("Output folder", default=config.output_dir)

    new_chunk = Prompt.ask("Default chunk size (bytes)", default=str(config.chunk_size))
    try:
        config.chunk_size = int(new_chunk)
    except ValueError:
        _warn("Invalid number, keeping the previous value")

    _info(
        "Error correction level: L (least robust, more data per QR code) / "
        "M (balanced, recommended) / Q / H (most robust, less data per QR code)"
    )
    level = Prompt.ask("Error correction level", choices=["L", "M", "Q", "H"], default=config.error_correction)
    config.error_correction = level

    warn_if_chunk_too_big(config.chunk_size, config.error_correction)

    # A short spinner for saving — writing the JSON is instant, but without
    # any feedback the jump from the previous table to the final "✓
    # Settings saved" felt too abrupt. A light pause with an explicit
    # status makes the save moment tangible rather than an instant blink.
    with console.status(f"[{_C_INFO}]Saving...[/{_C_INFO}]", spinner="dots"):
        config.save()
        time.sleep(0.2)

    _ok("Settings saved")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _cleanup_stale_temp_files():
    """Cleans up file_to_qr_* temp files from previous runs (e.g. leftovers
    after Ctrl+C mid-way through a camera frame capture). Files from the
    current process can't be deleted by mistake — they always contain
    os.getpid() of the active run. Not critical to functionality, just
    TEMP_DIR hygiene — errors are silently ignored and shouldn't block the
    program from starting.

    IMPORTANT: the assembly progress file (file_to_qr_camera_progress.json) is
    NOT touched here — it's meant to survive a program restart, that's its
    whole purpose (see _save_camera_progress / _load_camera_progress).
    """
    try:
        for p in TEMP_DIR.glob("file_to_qr_*"):
            if p == CAMERA_PROGRESS_FILE:
                continue
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def main():
    _cleanup_stale_temp_files()
    if not check_environment(require_camera=False):
        _err("Install the missing dependencies and run the script again.")
        sys.exit(1)

    show_intro_animation()

    config = Config.load()

    first_menu_show = True
    while True:
        try:
            # The screen is cleared before EVERY repeated menu display (but
            # not before the first one — so as not to wipe out the intro
            # banner that just rendered). Previously the screen just grew
            # downward with each return from a sub-mode — on a small Termux
            # terminal this quickly turned into an unreadable wall of old
            # tables, prompts, and logs from past operations. console.clear()
            # here doesn't "jitter" the picture — the previous result has
            # already been shown and read, and the next frame (the menu) is
            # drawn on a clean slate.
            if not first_menu_show:
                console.clear()
            console.print()
            show_main_menu(animate=first_menu_show)
            first_menu_show = False
            choice = Prompt.ask("Choice", choices=["0", "1", "2", "3", "4", "5"], default="0")

            if choice == "0":
                _info("See you!")
                break
            elif choice == "1":
                menu_encrypt(config)
            elif choice == "2":
                menu_decrypt_folder(config)
            elif choice == "3":
                menu_decrypt_camera(config)
            elif choice == "4":
                menu_settings(config)
            elif choice == "5":
                menu_scan_any_qr(config)

        except KeyboardInterrupt:
            console.print(f"\n[{_C_WARN}]Interrupted. Returning to the main menu (Ctrl+C again to exit).[/{_C_WARN}]")
            continue


if __name__ == "__main__":
    main()
