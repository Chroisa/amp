#!/usr/bin/env python3
"""
amp – animated man page (custom pager with a live animation region)

A less-style pager that overlays a scriptreplay (or built-in) animation
inside a marked rectangular region of the document.

Typical use as MANPAGER:

    export MANPAGER="python3 /path/to/amp.py"
    man somecommand

See --help and the amp(1) man page for full instructions.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import signal
import select
import termios
import tty
import subprocess
import threading
import re
import textwrap
from pathlib import Path
from typing import List, Optional, Tuple

VERSION = "1.0"

# ---------------------------------------------------------------------------
# CLI help / install / cleanup text
# ---------------------------------------------------------------------------

INSTALL_HINT = """\
Install / uninstall
-------------------
  ./install.sh            # print export MANPAGER=… for this shell
  ./install.sh --permanent
  ./uninstall.sh          # unset + remove from ~/.bashrc or ~/.zshrc

  Full details:  ./install.sh --help   and   ./uninstall.sh --help
"""

CLEANUP_HINT = ""  # covered by uninstall.sh


TERSE_HELP = """\
Usage: amp [options] [file]
       amp < formatted-text

One-shot with man(1):
  man -P /path/to/amp/amp topic

With paths on <<<ANIM_BOX timing=… typescript=…>>>, only MANPAGER is
required; ANIM_* variables are optional.  See amp(1) for path order.

Options:
  -h, --help      show this help
  -V, --version   show version
  --install-hint  install / uninstall summary

See install.sh for install tips.  Full documentation: amp(1).
"""

def print_help_and_install(path: str) -> None:
    print(TERSE_HELP)


# ---------------------------------------------------------------------------

def _truncate_ansi(s: str, width: int) -> str:
    """Return s truncated to `width` visible columns, never splitting a CSI."""
    out = []
    vis = 0
    i = 0
    n = len(s)
    while i < n and vis < width:
        if s[i] == "\033" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not (s[j].isalpha() or s[j] == "~"):
                j += 1
            if j < n:
                j += 1
            out.append(s[i:j])
            i = j
            continue
        out.append(s[i])
        vis += 1
        i += 1
    return "".join(out)


# Terminal helpers (pure ANSI, no curses)
# ---------------------------------------------------------------------------

def get_term_size(fd: Optional[int] = None) -> Tuple[int, int]:
    try:
        import shutil
        if fd is not None:
            # Query the real terminal, not a pipe
            try:
                import os
                import struct
                import fcntl
                import termios as _termios
                packed = fcntl.ioctl(fd, _termios.TIOCGWINSZ, b"\0\0\0\0\0\0\0\0")
                rows, cols = struct.unpack("HH", packed[:4])
                if rows > 0 and cols > 0:
                    return cols, rows
            except Exception:
                pass
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.columns, size.lines
    except Exception:
        return 80, 24

def hide_cursor():
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

def show_cursor():
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()

def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

def move_cursor(row: int, col: int):
    sys.stdout.write(f"\033[{row};{col}H")

def set_raw_mode(fd: int):
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    return old

def restore_mode(fd: int, old):
    termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ---------------------------------------------------------------------------
# Animation region detection & content scrubbing
# ---------------------------------------------------------------------------
#
# Preferred form – a real box with STATIC content in the middle so that
# ordinary pagers (less, more) still show useful text, while amp overwrites
# the rectangle with a live animation:
#
#     <<<ANIM_BOX w=40 h=6 id=A timing=demo.timing typescript=demo.typescript>>>
#            (static line 1 – visible in less)
#            (static line 2)
#            ...
#     <<<END_ANIM_BOX>>>
#
# Attributes on ANIM_BOX (all optional except that w and h are strongly
# recommended when using static content):
#     w=N  h=N  id=L
#     timing=PATH  typescript=PATH  divisor=N  loop=0|1
#
# Paths may be absolute (quoted or not), contain ~, or be relative.
# Relative names are resolved: cwd, then $AMP_DATA_DIR, $XDG_DATA_HOME/amp,
# ~/.local/share/amp, /usr/share/amp (and each */animations/).
# When both timing= and typescript= are given, ANIM_* variables are not required.
#
# Legacy form – solid runs of an uppercase letter (min width 8, min height 2)
# still work and map to ANIM_L_TIMING / ANIM_L_TYPESCRIPT.
#
# Maximum MAX_ANIMATIONS regions.  Marker lines are removed from the display;
# the interior is blanked under amp so the animation has a clean slate.

MARKER_START_LOOSE = re.compile(r"<<<ANIM_BOX\b[^>]*>>>")
MARKER_END = re.compile(r"<<<END_ANIM_BOX>>>")
MAX_ANIMATIONS = 5
MIN_FILLER_WIDTH = 8
MIN_FILLER_HEIGHT = 2
FILLER_RUN = re.compile(r"([A-Z])\1{" + str(MIN_FILLER_WIDTH - 1) + r",}")

ATTR_RE = re.compile(
    r"""(\w+)=(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
)


def parse_anim_box_attrs(line: str) -> dict:
    """Parse key=value attributes from an <<<ANIM_BOX ...>>> line."""
    attrs: dict = {}
    for m in ATTR_RE.finditer(line):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4)
        )
        attrs[key] = val
    return attrs


def default_data_dirs() -> List[str]:
    dirs = []
    env = os.environ.get("AMP_DATA_DIR")
    if env:
        dirs.append(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        dirs.append(os.path.join(xdg, "amp"))
    dirs.append(os.path.expanduser("~/.local/share/amp"))
    dirs.append("/usr/share/amp")
    return dirs


def resolve_anim_path(path: str) -> str:
    """Resolve an animation file path.

    Accepts:
      - absolute paths (quoted or unquoted)
      - paths with ~ (expanded)
      - relative paths (cwd first, then data directories)

    Search order for a relative name:
      1. as given (cwd)
      2. $AMP_DATA_DIR / $AMP_DATA_DIR/animations
      3. $XDG_DATA_HOME/amp / …/animations
      4. ~/.local/share/amp / …/animations
      5. /usr/share/amp / …/animations
    """
    if not path:
        return path
    # Expand ~ and environment-style vars the user might have written
    path = os.path.expanduser(path)
    if os.path.isabs(path) and os.path.isfile(path):
        return path
    # Relative: try as-is first (cwd)
    if os.path.isfile(path):
        return os.path.abspath(path)
    # Then data directories
    for d in default_data_dirs():
        cand = os.path.join(d, path)
        if os.path.isfile(cand):
            return cand
        cand2 = os.path.join(d, "animations", path)
        if os.path.isfile(cand2):
            return cand2
    return path  # leave unresolved – caller checks isfile


def _filler_run(line: str) -> Optional[Tuple[str, int, int]]:
    best = None
    for m in FILLER_RUN.finditer(line):
        letter = m.group(1)
        col, width = m.start(), m.end() - m.start()
        if best is None or width > best[2]:
            best = (letter, col, width)
    return best


def _body_indent(lines: List[str]) -> int:
    """Minimum leading-space indent among non-empty body lines."""
    indents = []
    for ln in lines:
        if not ln.strip():
            continue
        indents.append(len(ln) - len(ln.lstrip(" ")))
    return min(indents) if indents else 0


def find_all_anim_blocks(lines: List[str]) -> List[dict]:
    """
    Find up to MAX_ANIMATIONS animation regions.

    Each dict keys:
      letter, first, last, col, width, height, start_marker,
      timing, typescript, divisor, loop
    """
    blocks: List[dict] = []
    used_lines: set = set()
    used_letters: set = set()

    def next_letter() -> str:
        for L in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if L not in used_letters:
                return L
        return "Z"

    def add_block(**kw):
        if len(blocks) >= MAX_ANIMATIONS:
            return
        letter = kw["letter"]
        if letter in used_letters:
            # allow only one region per letter id
            return
        used_letters.add(letter)
        for ln in range(kw["first"], kw["last"] + 1):
            used_lines.add(ln)
        if kw.get("start_marker", -1) >= 0:
            used_lines.add(kw["start_marker"])
        blocks.append(kw)

    # ---- Pass 1: <<<ANIM_BOX ...>>> … <<<END_ANIM_BOX>>> with static body ----
    for i, line in enumerate(lines):
        if i in used_lines:
            continue
        if not MARKER_START_LOOSE.search(line):
            continue
        attrs = parse_anim_box_attrs(line)
        try:
            width = int(attrs["w"]) if "w" in attrs else None
            height = int(attrs["h"]) if "h" in attrs else None
        except ValueError:
            width = height = None
        letter = (attrs.get("id") or attrs.get("letter") or next_letter()).upper()[:1]
        timing = attrs.get("timing") or attrs.get("t")
        typescript = attrs.get("typescript") or attrs.get("ts")
        divisor = attrs.get("divisor")
        loop = attrs.get("loop")

        # Collect body until END marker or height lines
        body_idx: List[int] = []
        j = i + 1
        while j < len(lines):
            if MARKER_END.search(lines[j]):
                break
            if MARKER_START_LOOSE.search(lines[j]):
                break
            body_idx.append(j)
            j += 1
            if height is not None and len(body_idx) >= height:
                break

        if not body_idx:
            continue

        # Find END marker; any extra lines before it (beyond h) are skipped
        # so leftover static rows do not leak into the display.
        end_marker = -1
        k = j
        extra = []
        while k < len(lines):
            if MARKER_END.search(lines[k]):
                end_marker = k
                used_lines.add(k)
                for e in extra:
                    used_lines.add(e)
                break
            if MARKER_START_LOOSE.search(lines[k]):
                break
            extra.append(k)
            k += 1
            # do not scan forever – one screen of slack is enough
            if len(extra) > 50:
                break

        if height is None:
            height = len(body_idx)
        else:
            height = min(height, len(body_idx))
            body_idx = body_idx[:height]

        body_lines = [lines[k] for k in body_idx]
        col = _body_indent(body_lines)
        if width is None:
            # width = max visible length of body after indent
            width = 0
            for bl in body_lines:
                width = max(width, max(0, len(bl.rstrip()) - col))
            width = max(width, MIN_FILLER_WIDTH)

        add_block(
            letter=letter,
            first=body_idx[0],
            last=body_idx[0] + height - 1,
            col=col,
            width=width,
            height=height,
            start_marker=i,
            end_marker=end_marker,
            timing=timing,
            typescript=typescript,
            divisor=divisor,
            loop=loop,
        )

    # ---- Pass 2: legacy solid letter runs (no static content) ----
    i = 0
    while i < len(lines) and len(blocks) < MAX_ANIMATIONS:
        if i in used_lines:
            i += 1
            continue
        run = _filler_run(lines[i])
        if not run:
            i += 1
            continue
        letter, col, width = run
        if letter in used_letters:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and j not in used_lines:
            r = _filler_run(lines[j])
            if not r or r[0] != letter or abs(r[1] - col) > 2:
                break
            width = max(width, r[2])
            j += 1
        height = j - i
        if height >= MIN_FILLER_HEIGHT and width >= MIN_FILLER_WIDTH:
            add_block(
                letter=letter,
                first=i,
                last=j - 1,
                col=col,
                width=width,
                height=height,
                start_marker=-1,
                end_marker=-1,
                timing=None,
                typescript=None,
                divisor=None,
                loop=None,
            )
            i = j
        else:
            i += 1

    return blocks


def scrub_anim_region(raw_lines: List[str]):
    """
    Remove marker lines; blank the interior of each box so amp has a clean
    rectangle.  Static body text is kept only for non-amp pagers – under amp
    it is replaced with spaces (same indent + width).
    """
    blocks = find_all_anim_blocks(raw_lines)
    if not blocks:
        return list(raw_lines), []

    skip = set()
    for b in blocks:
        if b.get("start_marker", -1) >= 0:
            skip.add(b["start_marker"])
        if b.get("end_marker", -1) >= 0:
            skip.add(b["end_marker"])

    line_to_block = {}
    for b in blocks:
        for ln in range(b["first"], b["last"] + 1):
            line_to_block[ln] = b

    display: List[str] = []
    first_display = {id(b): None for b in blocks}

    for idx, line in enumerate(raw_lines):
        if idx in skip:
            continue
        if idx in line_to_block:
            b = line_to_block[idx]
            col, width = b["col"], b["width"]
            # Blank the animation rectangle; preserve pure indent before col
            prefix = " " * col
            blank = prefix + (" " * width)
            if first_display[id(b)] is None:
                first_display[id(b)] = len(display)
            display.append(blank)
        else:
            display.append(line)

    boxes = []
    for b in blocks:
        fd = first_display[id(b)]
        if fd is not None:
            boxes.append({
                "line": fd,
                "col": b["col"],
                "width": b["width"],
                "height": b["height"],
                "letter": b["letter"],
                "timing": b.get("timing"),
                "typescript": b.get("typescript"),
                "divisor": b.get("divisor"),
                "loop": b.get("loop"),
            })
    return display, boxes


def wrap_content(lines: List[str], width: int, boxes=None):
    if width < 8:
        width = 8
    if boxes is None:
        boxes = []

    box_line_set = set()
    for b in boxes:
        bl, bh = b["line"], b["height"]
        box_line_set.update(range(bl, bl + bh))

    out: List[str] = []
    new_starts = {}

    for idx, line in enumerate(lines):
        if idx in box_line_set:
            for b in boxes:
                if b["line"] <= idx < b["line"] + b["height"] and b["line"] not in new_starts:
                    new_starts[b["line"]] = len(out)
            out.append(line)
            continue
        if not line:
            out.append("")
            continue
        wrapped = textwrap.wrap(
            line,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=True,
        )
        if not wrapped:
            out.append("")
        else:
            out.extend(wrapped)

    new_boxes = []
    for b in boxes:
        if b["line"] in new_starts:
            nb = dict(b)
            nb["line"] = new_starts[b["line"]]
            new_boxes.append(nb)
    return out, new_boxes


class Animation:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self._frame = 0
        self._lock = threading.Lock()
        self._current_lines: List[str] = [" " * width for _ in range(height)]
        self._stop = False

    def stop(self):
        self._stop = True

    def get_frame(self) -> List[str]:
        with self._lock:
            return list(self._current_lines)

    def _set_frame(self, lines: List[str]):
        out = []
        for i in range(self.height):
            if i < len(lines):
                s = lines[i]
            else:
                s = " " * self.width
            out.append(s)
        with self._lock:
            self._current_lines = out


class BuiltinSpinner(Animation):
    def __init__(self, width: int, height: int):
        super().__init__(width, height)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        spinner = "|/-\\"
        colours = ["\033[31m", "\033[33m", "\033[32m", "\033[36m", "\033[34m", "\033[35m"]
        msg = " set ANIM_L_TIMING & ANIM_L_TYPESCRIPT (L = filler letter) "
        while not self._stop:
            ch = spinner[self._frame % len(spinner)]
            col = colours[self._frame % len(colours)]
            reset = "\033[0m"
            lines = []
            for r in range(self.height):
                if r == self.height // 2:
                    text = f" {ch} {msg} {ch} "
                    pad = max(0, (self.width - len(text)) // 2)
                    lines.append(" " * pad + col + text + reset)
                else:
                    lines.append(col + ("·" * self.width) + reset)
            self._set_frame(lines)
            self._frame += 1
            time.sleep(0.12)


class ScriptReplayAnimation(Animation):
    """Play a script(1) timing+typescript into a fixed box; loop by default.

    Parses the files itself (no external scriptreplay process) so that
    clear-screen framed multi-line events from amp-trim are applied as a
    whole frame rather than being scrolled line-by-line.
    """

    _CLEAR_RE = re.compile(r"\033\[[0-9;]*[HJK]")

    def __init__(self, width: int, height: int,
                 timing: str, typescript: str, divisor: float = 1.0,
                 loop: bool = True):
        super().__init__(width, height)
        self.timing = timing
        self.typescript = typescript
        self.divisor = max(0.01, float(divisor) if divisor else 1.0)
        self.loop = loop
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _load_events(self) -> List[Tuple[float, bytes]]:
        events: List[Tuple[float, bytes]] = []
        try:
            with open(self.timing, "r", encoding="utf-8", errors="replace") as tf, \
                 open(self.typescript, "rb") as sf:
                for line in tf:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    try:
                        delay = float(parts[0])
                        count = int(parts[1])
                    except ValueError:
                        continue
                    data = sf.read(count)
                    if len(data) < count:
                        break
                    events.append((delay, data))
        except OSError as e:
            self._set_frame(
                [f"amp: cannot read {e.filename or self.timing}: {e.strerror}"[: self.width]]
                + [""] * (self.height - 1)
            )
            return []
        return events

    def _init_screen(self):
        """Virtual screen of (sgr, char) cells plus pending incomplete ESC.

        The screen is taller than the box so content that exceeds the box
        height is still captured; we display the bottom rows so the fire
        base (or other bottom-aligned content) stays visible.
        """
        self._vheight = self.height + 8  # extra rows for overflow
        self._screen = [[("", " ") for _ in range(self.width)] for _ in range(self._vheight)]
        self._sgr = ""
        self._row = 0
        self._col = 0
        self._max_row = 0  # highest row that received content
        self._carry = ""  # incomplete ESC / UTF-8 across events

    def _capture_frame(self) -> List[str]:
        """Build the display lines from the current virtual screen, preferring the bottom."""
        content_rows = self._max_row + 1
        start = max(0, content_rows - self.height)
        lines = []
        for r in range(start, start + self.height):
            if r >= self._vheight:
                break
            parts = []
            for c in range(self.width):
                sgr, ch = self._screen[r][c]
                parts.append(f"{sgr}{ch}")
            line = "".join(parts) + "\033[0m"
            lines.append(line)
        # Pad if short
        while len(lines) < self.height:
            lines.append(" " * self.width + "\033[0m")
        return lines

    def _bytes_to_lines(self, data: bytes) -> None:
        """Feed one event into the virtual screen; return current frame lines.

        Handles split CSI sequences (they may span events) so partial codes
        never appear as text.  Clears reset the screen.  Output is exactly
        height lines, each visually width columns, with intact SGR.
        """
        if not hasattr(self, "_screen"):
            self._init_screen()

        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("latin1", errors="replace")
        text = self._carry + text
        self._carry = ""

        # Skip the script(1) header line if present (it is not display content)
        if text.startswith("Script started"):
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1:]
            else:
                text = ""

        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\033":
                j = i + 1
                if j >= n:
                    self._carry = text[i:]
                    break
                if text[j] == "[":
                    j += 1
                    while j < n and not (text[j].isalpha() or text[j] == "~"):
                        j += 1
                    if j >= n:
                        self._carry = text[i:]
                        break
                    final = text[j]
                    params = text[i + 2:j]
                    seq = text[i:j + 1]
                    j += 1
                    if final == "m":
                        self._sgr = seq
                        i = j
                        continue
                    if final in ("H", "f"):
                        parts = params.split(";") if params else []
                        try:
                            r = int(parts[0]) if parts and parts[0] else 1
                        except ValueError:
                            r = 1
                        try:
                            c = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                        except ValueError:
                            c = 1
                        self._row = max(0, min(self._vheight - 1, r - 1))
                        self._col = max(0, min(self.width - 1, c - 1))
                        i = j
                        continue
                    if final == "J":
                        # clear display – capture the completed frame first
                        if params in ("", "2", "3"):
                            if self._max_row > 0:
                                self._set_frame(self._capture_frame())
                            self._screen = [[("", " ") for _ in range(self.width)] for _ in range(self._vheight)]
                            self._row = self._col = 0
                            self._max_row = 0
                        i = j
                        continue
                    if final == "K":
                        # erase to end of line
                        for c in range(self._col, self.width):
                            self._screen[self._row][c] = ("", " ")
                        i = j
                        continue
                    # other CSI – ignore
                    i = j
                    continue
                # other ESC – skip ESC + next if present
                i = j + 1 if j < n else j
                continue

            if ch == "\r":
                self._col = 0
                i += 1
                continue
            if ch == "\n":
                self._row = min(self._vheight - 1, self._row + 1)
                self._col = 0
                i += 1
                continue
            if ch == "\b":
                if self._col > 0:
                    self._col -= 1
                i += 1
                continue
            if ord(ch) < 32 and ch != "\t":
                i += 1
                continue

            # printable
            if 0 <= self._row < self._vheight and 0 <= self._col < self.width:
                self._screen[self._row][self._col] = (self._sgr, ch)
                if self._row > self._max_row:
                    self._max_row = self._row
            self._col += 1
            # Do NOT auto-wrap here.  The source uses explicit newlines
            # (and CUP).  Auto-wrapping would double-advance the row when
            # a line is exactly `width` columns followed by \n, which
            # skipped every other row of the fire animation.
            if self._col > self.width:
                self._col = self.width  # clamp; further chars in this row are dropped
            i += 1

        # Frame capture now happens on clear (before reset) so complete frames
        # are recorded.  Partial events simply accumulate in the virtual screen.

    def _run_once(self) -> bool:
        events = self._load_events()
        if not events:
            return False
        # Reset screen for a clean play-through
        self._init_screen()
        for delay, data in events:
            if self._stop:
                return False
            wait = max(0.0, delay / self.divisor)
            if wait > 0:
                # sleep in small slices so stop is responsive
                end = time.time() + wait
                while not self._stop and time.time() < end:
                    time.sleep(min(0.05, max(0.0, end - time.time())))
            if self._stop:
                return False
            if not data:
                continue
            # Always feed; clears will capture the preceding complete frame
            self._bytes_to_lines(data)
        # Capture any final frame that was not followed by a clear
        if self._max_row > 0:
            self._set_frame(self._capture_frame())
        return not self._stop

    def _run(self):
        while not self._stop:
            ok = self._run_once()
            if self._stop or not self.loop or not ok:
                break
            time.sleep(0.05)

    def stop(self):
        super().stop()


HELP_TEXT = """\
AMP – ANIMATED MAN PAGE – HELP
==============================

Navigation
  space / PgDn page forward
  b / PgUp     page backward
  j / ↓        line forward
  k / ↑        line backward
  g / Home     go to top
  G / End      go to bottom

Search
  /            start a search (type the string, then Enter)
  n            next match
  N            previous match

Other
  h  or  ?     this help screen (animation is paused)
  q            quit

Animation
  Up to five rectangles of uppercase filler letters (A–Z) are replaced by
  live animations.  Marker lines (<<<ANIM_BOX>>> / <<<END_ANIM_BOX>>>) and
  the filler characters are removed from the display.  Colour / caca-style
  frames are supported.  Animations loop by default.

  For each letter L used as filler, set:

      ANIM_L_TIMING      path to a script(1) timing file
      ANIM_L_TYPESCRIPT  path to the corresponding typescript
      ANIM_L_DIVISOR     optional speed multiplier (default 1.0)
      ANIM_L_LOOP        1/0 (default 1 = loop)

  Example: a block of A's uses ANIM_A_TIMING + ANIM_A_TYPESCRIPT.

  Prefer preparing typescripts with amp-trim(1) so clear-screen sequences
  are removed and the content is bounded to the box size.

Press any key to return to the document.
"""

# ---------------------------------------------------------------------------
# Main pager
# ---------------------------------------------------------------------------

class AmpPager:
    def __init__(self, content: str, tty_fd: Optional[int] = None):
        self.raw_lines = content.splitlines()
        self.cols, self.rows = get_term_size()
        self.view_height = max(1, self.rows - 1)
        # Scrub markers / filler BEFORE wrapping so the static page never
        # shows <<<ANIM_BOX>>>, <<<END_ANIM_BOX>>>, or filler letters.
        scrubbed, boxes = scrub_anim_region(self.raw_lines)
        self.lines, self.boxes = wrap_content(scrubbed, self.cols, boxes)
        self.top = 0
        self.anims: List[Animation] = []
        self._old_term = None
        # Interactive I/O must use a real terminal.  When man(1) pipes the
        # formatted page on stdin, stdin is not a tty – open /dev/tty instead
        # (same approach as less).
        if tty_fd is not None:
            self._fd = tty_fd
        elif sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
        else:
            try:
                self._tty_file = open("/dev/tty", "r+b", buffering=0)
                self._fd = self._tty_file.fileno()
            except OSError as e:
                print(
                    f"amp: cannot open /dev/tty for interactive input ({e}).\n"
                    "     When used as MANPAGER, a controlling terminal is required.",
                    file=sys.stderr,
                )
                sys.exit(2)
        self.mode = "normal"
        self.search_query = ""
        self.search_matches: List[int] = []
        self.search_idx = -1

    def setup_animation(self):
        """
        Bind each box to an animation source.

        Resolution order for timing/typescript paths:
          1. Attributes on <<<ANIM_BOX timing=… typescript=…>>>
          2. Environment ANIM_L_TIMING / ANIM_L_TYPESCRIPT (L = id)
          3. Legacy ANIM_TIMING / ANIM_TYPESCRIPT for id Q

        Paths are resolved via resolve_anim_path() (AMP_DATA_DIR, etc.).
        """
        self.anims = []
        if not self.boxes:
            return

        for b in self.boxes:
            letter = b["letter"]
            w, h = b["width"], b["height"]
            timing = b.get("timing") or os.environ.get(f"ANIM_{letter}_TIMING")
            typescript = b.get("typescript") or os.environ.get(f"ANIM_{letter}_TYPESCRIPT")
            if letter == "Q":
                timing = timing or os.environ.get("ANIM_TIMING")
                typescript = typescript or os.environ.get("ANIM_TYPESCRIPT")
            if timing:
                timing = resolve_anim_path(timing)
            if typescript:
                typescript = resolve_anim_path(typescript)

            divisor_s = (
                b.get("divisor")
                or os.environ.get(f"ANIM_{letter}_DIVISOR")
                or os.environ.get("ANIM_DIVISOR", "1.0")
            )
            loop_s = (
                b.get("loop")
                or os.environ.get(f"ANIM_{letter}_LOOP")
                or "1"
            )
            loop = str(loop_s).lower() not in ("0", "false", "no", "off")
            try:
                divisor = float(divisor_s)
            except (ValueError, TypeError):
                divisor = 1.0

            if timing and typescript and os.path.isfile(timing) and os.path.isfile(typescript):
                self.anims.append(
                    ScriptReplayAnimation(w, h, timing, typescript, divisor, loop=loop)
                )
            else:
                self.anims.append(BuiltinSpinner(w, h))

    def visible_slice(self) -> List[str]:
        return self.lines[self.top : self.top + self.view_height]

    def _draw_status(self, extra: str = ""):
        move_cursor(self.rows, 1)
        if self.mode == "search":
            status = f" /{self.search_query}_"
        elif self.mode == "help":
            status = " HELP – press any key to return "
        else:
            status = (
                f" amp  line {self.top+1}/{len(self.lines)}  |  "
                f"q=quit  space/b=page  j/k=line  /=search  h=help  "
                f"(animation) {extra}"
            )
        sys.stdout.write(status[: self.cols].ljust(self.cols))
        sys.stdout.flush()

    def draw(self):
        clear_screen()
        if self.mode == "help":
            help_lines = HELP_TEXT.splitlines()
            for i, line in enumerate(help_lines[: self.view_height]):
                sys.stdout.write(line[: self.cols].ljust(self.cols) + "\n")
            self._draw_status()
            return

        visible = self.visible_slice()
        for i, line in enumerate(visible):
            display = line
            if self.search_query and self.search_query.lower() in line.lower():
                display = re.sub(
                    re.escape(self.search_query),
                    lambda m: f"\033[7m{m.group(0)}\033[27m",
                    line,
                    flags=re.IGNORECASE,
                )
            sys.stdout.write(display[: self.cols].ljust(self.cols) + "\n")

        if self.mode == "normal" and self.boxes and self.anims:
            for box, anim in zip(self.boxes, self.anims):
                line_idx, col, w, h = box["line"], box["col"], box["width"], box["height"]
                screen_row = line_idx - self.top + 1
                if screen_row + h - 1 < 1 or screen_row > self.view_height:
                    continue
                frame = anim.get_frame()
                for r in range(h):
                    sr = screen_row + r
                    if 1 <= sr <= self.view_height:
                        fline = frame[r] if r < len(frame) else ""
                        # Ensure we do not write past the box; fline is already
                        # width-bounded by the virtual screen, but pad if short
                        plain = re.sub(r"\033\[[0-9;]*[a-zA-Z]", "", fline)
                        if len(plain) < w:
                            fline = fline + (" " * (w - len(plain)))
                        elif len(plain) > w:
                            # Truncate to w visible columns without splitting CSI
                            fline = _truncate_ansi(fline, w)
                        move_cursor(sr, col + 1)
                        sys.stdout.write(fline)
                        sys.stdout.write("\033[0m")
        self._draw_status()

    def do_search(self, query: str):
        self.search_query = query
        self.search_matches = [
            i for i, ln in enumerate(self.lines)
            if query.lower() in ln.lower()
        ]
        self.search_idx = -1
        if self.search_matches:
            self.next_match()

    def next_match(self, reverse: bool = False):
        if not self.search_matches:
            return
        if reverse:
            self.search_idx = (self.search_idx - 1) % len(self.search_matches)
        else:
            self.search_idx = (self.search_idx + 1) % len(self.search_matches)
        target = self.search_matches[self.search_idx]
        self.top = max(0, min(target - self.view_height // 3,
                              len(self.lines) - self.view_height))


    def _read_key(self) -> str:
        """Read one key, decoding CSI sequences (arrows, PgUp/PgDn, Home/End)."""
        ch = os.read(self._fd, 1).decode(errors="ignore")
        if ch != "\x1b":
            if ch in ("\x7f", "\b"):
                return "backspace"
            return ch
        seq = ch
        try:
            while True:
                r, _, _ = select.select([self._fd], [], [], 0.05)
                if not r:
                    break
                nxt = os.read(self._fd, 1).decode(errors="ignore")
                if not nxt:
                    break
                seq += nxt
                if nxt.isalpha() or nxt == "~":
                    break
                if len(seq) > 8:
                    break
        except Exception:
            pass
        table = {
            "\x1b[A": "up", "\x1bOA": "up",
            "\x1b[B": "down", "\x1bOB": "down",
            "\x1b[C": "right", "\x1bOC": "right",
            "\x1b[D": "left", "\x1bOD": "left",
            "\x1b[5~": "pgup", "\x1b[6~": "pgdn",
            "\x1b[H": "home", "\x1b[1~": "home", "\x1bOH": "home",
            "\x1b[F": "end", "\x1b[4~": "end", "\x1bOF": "end",
        }
        if seq in table:
            return table[seq]
        if seq == "\x1b":
            return "esc"
        return seq

    def run(self):
        # Prefer geometry of the controlling terminal
        try:
            self.cols, self.rows = get_term_size(self._fd)
            self.view_height = max(1, self.rows - 1)
            scrubbed, boxes = scrub_anim_region(self.raw_lines)
            self.lines, self.boxes = wrap_content(scrubbed, self.cols, boxes)
        except Exception:
            pass
        self.setup_animation()
        hide_cursor()
        try:
            self._old_term = set_raw_mode(self._fd)
        except termios.error as e:
            print(
                f"amp: cannot set terminal mode on fd {self._fd}: {e}\n"
                "     A real terminal (e.g. /dev/tty) is required.",
                file=sys.stderr,
            )
            sys.exit(2)

        def cleanup(*_):
            for a in getattr(self, "anims", []) or []:
                a.stop()
            show_cursor()
            if self._old_term:
                restore_mode(self._fd, self._old_term)
            clear_screen()
            sys.exit(0)

        signal.signal(signal.SIGINT, cleanup)
        signal.signal(signal.SIGTERM, cleanup)

        try:
            self.draw()
            while True:
                r, _, _ = select.select([self._fd], [], [], 0.08)
                if r:
                    key = self._read_key()
                    if self.mode == "help":
                        self.mode = "normal"
                        self.draw()
                        continue
                    if self.mode == "search":
                        if key in ("\r", "\n", "enter"):
                            self.mode = "normal"
                            if self.search_query:
                                self.do_search(self.search_query)
                            self.draw()
                        elif key in ("backspace", "\x7f", "\b"):
                            self.search_query = self.search_query[:-1]
                            self.draw()
                        elif key == "esc":
                            self.search_query = ""
                            self.mode = "normal"
                            self.draw()
                        elif len(key) == 1 and ord(key) >= 32:
                            self.search_query += key
                            self.draw()
                        continue
                    if key in ("q", "Q"):
                        break
                    elif key in (" ", "pgdn"):
                        self.top = min(
                            self.top + self.view_height - 1,
                            max(0, len(self.lines) - self.view_height),
                        )
                    elif key in ("b", "pgup"):
                        self.top = max(0, self.top - (self.view_height - 1))
                    elif key in ("j", "\n", "down"):
                        self.top = min(
                            self.top + 1,
                            max(0, len(self.lines) - self.view_height),
                        )
                    elif key in ("k", "up"):
                        self.top = max(0, self.top - 1)
                    elif key in ("g", "home"):
                        self.top = 0
                    elif key in ("G", "end"):
                        self.top = max(0, len(self.lines) - self.view_height)
                    elif key == "/":
                        self.mode = "search"
                        self.search_query = ""
                    elif key == "n":
                        self.next_match(reverse=False)
                    elif key == "N":
                        self.next_match(reverse=True)
                    elif key in ("h", "?"):
                        self.mode = "help"
                    self.draw()
                else:
                    if self.mode == "normal":
                        self.draw()
        finally:
            cleanup()


def resolve_self_path() -> str:
    try:
        return os.path.abspath(__file__)
    except Exception:
        return "amp.py"


def main() -> None:
    path = resolve_self_path()

    parser = argparse.ArgumentParser(
        prog="amp",
        add_help=False,
        description="amp – animated man page pager",
    )
    parser.add_argument("-h", "--help", action="store_true",
                        help="show help and installation/cleanup instructions")
    parser.add_argument("-V", "--version", action="store_true",
                        help="show version")
    parser.add_argument("--install-hint", action="store_true",
                        help="print only the MANPAGER installation snippet")
    parser.add_argument("--cleanup-hint", action="store_true",
                        help="print only undo / cleanup instructions")
    parser.add_argument("file", nargs="?", default=None,
                        help="formatted text file (default: read stdin)")

    # Parse known args so extra tokens from man(1) do not crash us
    args, unknown = parser.parse_known_args()

    if args.help:
        print_help_and_install(path)
        sys.exit(0)
    if args.version:
        print(f"amp {VERSION}")
        sys.exit(0)
    if args.install_hint:
        print(INSTALL_HINT)
        sys.exit(0)
    if args.cleanup_hint:
        print("Use:  ./uninstall.sh   (see ./uninstall.sh --help)")
        sys.exit(0)

    # Friendly handling of slightly wrong usage
    if unknown and not args.file:
        # e.g. user typed: amp --foo
        print(f"amp: unrecognised argument(s): {' '.join(unknown)}", file=sys.stderr)
        print("Try 'amp --help' for usage and installation instructions.", file=sys.stderr)
        sys.exit(2)

    # Obtain content
    content = ""
    if args.file and args.file != "-":
        if not os.path.isfile(args.file):
            print(f"amp: cannot open file: {args.file}", file=sys.stderr)
            print("Try 'amp --help' for usage.", file=sys.stderr)
            sys.exit(2)
        try:
            with open(args.file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            print(f"amp: failed to read {args.file}: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        # stdin – when used as MANPAGER, man pipes the page here
        if sys.stdin.isatty() and not args.file:
            # Interactive terminal, no file, no piped content → friendly help
            print("amp: no input on stdin and no file given.", file=sys.stderr)
            print(file=sys.stderr)
            print_help_and_install(path)
            sys.exit(2)
        content = sys.stdin.read()

    if not content.strip():
        print("amp: no page content to display.", file=sys.stderr)
        print(file=sys.stderr)
        print_help_and_install(path)
        sys.exit(2)

    pager = AmpPager(content)
    try:
        pager.run()
    except termios.error as e:
        print(f"amp: terminal error: {e}", file=sys.stderr)
        print("     Tip: run under a real terminal, e.g.", file=sys.stderr)
        print('       man -P "python3 /path/to/amp.py" topic', file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
