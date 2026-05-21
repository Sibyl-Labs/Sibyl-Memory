"""ASCII banner for sibyl-memory-cli.

Prints the SIBYL wordmark in ANSI Shadow boxchars with a 24-bit truecolor
vertical gradient flowing from cream/white at the top through warm gold
to deep ochre at the bottom: aligned with the lab visual identity per
the operator's brand-discipline rule (creme palette, deep-ochre accent).

Gracefully degrades:
  - NO_COLOR env var set       → plain text fallback
  - stdout is not a TTY        → plain text fallback (or skip entirely)
  - TERM=dumb                  → plain text fallback

Truecolor support is detected via $COLORTERM (truecolor / 24bit): most
modern terminals (iTerm2, Alacritty, Kitty, wezterm, Windows Terminal,
modern xterm builds, Ghostty) advertise it. Falls back to 256-color
gradient when not available.
"""
from __future__ import annotations

import os
import sys

# ANSI Shadow rendering of "SIBYL": 6 rows, 41 cols. Each row gets its
# own gradient color (top = pale cream/white, bottom = deep ochre).
_LINES = (
    "███████╗██╗██████╗ ██╗   ██╗██╗     ",
    "██╔════╝██║██╔══██╗╚██╗ ██╔╝██║     ",
    "███████╗██║██████╔╝ ╚████╔╝ ██║     ",
    "╚════██║██║██╔══██╗  ╚██╔╝  ██║     ",
    "███████║██║██████╔╝   ██║   ███████╗",
    "╚══════╝╚═╝╚═════╝    ╚═╝   ╚══════╝",
)

# Vertical gradient · cream → gold → deep ochre. One RGB tuple per row.
# Tuned against the SIBYL palette: --paper #f5f1e6 (top blend),
# --accent #8a6a2a (mid-bottom), with extra highlight + shadow stops
# to give the wordmark visible dimension.
_GRADIENT = (
    (253, 251, 245),   # almost white, slight cream      (top highlight)
    (244, 229, 184),   # pale gold                       (upper)
    (224, 194, 119),   # mid gold                        (upper-mid)
    (184, 146,  73),   # rich ochre gold                 (mid)
    (138, 106,  42),   # deep ochre · brand --accent     (lower)
    (106,  79,  31),   # deepest                         (bottom shadow)
)

_TAGLINE = "memory you can hold in your hand"
_ATTRIBUTION = "a Sibyl Labs LLC Product. Agentic Infrastructure and Memory Products"


def _supports_truecolor() -> bool:
    """Detect 24-bit color support. Conservative: fall back gracefully."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    if not sys.stdout.isatty():
        return False
    colorterm = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return True
    # Many modern terminals don't set COLORTERM but do support truecolor.
    # Recognize the well-behaved emitters.
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in {"iterm.app", "wezterm", "ghostty", "vscode", "tabby"}:
        return True
    term = os.environ.get("TERM", "").lower()
    if any(k in term for k in ("256color", "kitty", "alacritty", "xterm-direct")):
        return True
    return False


def _color_supported() -> bool:
    """Plain ANSI color (3/4-bit). Stricter than truecolor."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return sys.stdout.isatty()


def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


_RESET = "\033[0m"


def render_banner(*, force_color: bool | None = None) -> str:
    """Return the banner as a string ready to print.

    Args:
        force_color: Override auto-detection. None = auto, True = force
            truecolor, False = force plain text. Useful for testing.
    """
    use_truecolor = force_color if force_color is not None else _supports_truecolor()

    if not use_truecolor:
        # Plain text: still visually clean, just no color.
        body = "\n".join("  " + line for line in _LINES)
        tagline = f"\n  {_TAGLINE}"
        attribution = f"\n  {_ATTRIBUTION}\n"
        return body + tagline + attribution

    # Colored: apply per-row gradient.
    colored_lines = []
    for line, (r, g, b) in zip(_LINES, _GRADIENT):
        colored_lines.append(f"  {_rgb(r, g, b)}{line}{_RESET}")

    body = "\n".join(colored_lines)
    # Tagline in the deepest gold: present, but not competing with the wordmark.
    r, g, b = _GRADIENT[-1]
    tagline = f"\n  {_rgb(r, g, b)}{_TAGLINE}{_RESET}"
    # Attribution dimmer still: a half-step below the tagline so the hierarchy
    # reads SIBYL > tagline > attribution at a glance. ANSI dim (\033[2m) gives
    # ~55% perceived opacity across the supported terminals.
    attribution = f"\n  \033[2m{_rgb(r, g, b)}{_ATTRIBUTION}{_RESET}\n"
    return body + tagline + attribution


def print_banner(*, force_color: bool | None = None) -> None:
    """Print the banner. Safe to call unconditionally; honors NO_COLOR + TTY checks."""
    print(render_banner(force_color=force_color))
