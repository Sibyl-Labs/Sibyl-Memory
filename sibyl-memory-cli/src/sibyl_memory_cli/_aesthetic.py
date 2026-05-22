"""Shared visual identity for the sibyl CLI surface.

Sister module to `_banner.py`. Where the banner is the identity-reveal
moment for `sibyl init`, this module supplies the granular building
blocks every subcommand uses to share one coherent look:

  - 24-bit-truecolor → 256-color → plain-text degradation cascade
  - Brand palette derived from the lab creme paper face (rule 46)
  - Letter-spaced eyebrow labels, gradient titles, ASCII rule dividers
  - Key/value rows, status chips, success/warn/error glyphs
  - Pulsing accents for live states (activation, upgrade, watching)

Voice constraint: precise, editorial, restrained. Gradients flow over
2–3 stops max. No rainbow. The terminal is paper.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable

# ─── Palette (RGB · derived from rule 46 creme-paper tokens) ─────────
# Names map 1:1 to CSS custom properties on lab artifacts.

PAPER       = (245, 241, 230)   # --paper        — foreground accent on dark
PAPER_DEEP  = (237, 230, 211)   # --paper-deep   — depth on creme
CARD        = (253, 251, 245)   # --card         — slightly lifted creme
INK         = (21,  17,  10)    # --ink          — main text on creme
INK_SOFT    = (44,  39,  29)    # --ink-soft     — body text
INK_MUTE    = (106, 99,  86)    # --ink-mute     — secondary text
INK_FAINT   = (152, 145, 127)   # --ink-faint    — tertiary text
RULE        = (216, 208, 187)   # --rule         — hairline
RULE_STRONG = (184, 174, 147)   # --rule-strong  — emphasised hairline
ACCENT      = (138, 106, 42)    # --accent       — ochre highlight
ACCENT_WARM = (160, 132, 56)    # --accent-warm  — softer ochre
ACCENT_GOLD = (224, 194, 119)   # mid gold       — gradient bridge
ACCENT_PALE = (244, 229, 184)   # pale gold      — gradient top
JADE        = (45,  110, 106)   # --jade         — cool counterpoint
PULSE       = (29,  138, 130)   # --pulse        — brighter jade (live signal)
ERROR       = (162, 58,  42)    # --error        — measured red

# Status glyphs (Unicode, terminal-safe in modern fonts)
GLYPH_OK    = "✓"
GLYPH_WARN  = "⚠"
GLYPH_ERR   = "✗"
GLYPH_DOT   = "·"
GLYPH_ARROW = "→"
GLYPH_BULLET = "▸"


# ─── Terminal capability detection ────────────────────────────────────

def supports_truecolor() -> bool:
    """24-bit RGB ANSI. Same heuristic as _banner.py."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    # SIBYL_FORCE_COLOR=1 — explicit override for non-tty rendering
    # (CI logs, doc captures, dev inspection inside the Claude harness).
    if os.environ.get("SIBYL_FORCE_COLOR") == "1":
        return True
    if not sys.stdout.isatty():
        return False
    colorterm = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return True
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in {"iterm.app", "wezterm", "ghostty", "vscode", "tabby"}:
        return True
    term = os.environ.get("TERM", "").lower()
    if any(k in term for k in ("256color", "kitty", "alacritty", "xterm-direct")):
        return True
    return False


def supports_color() -> bool:
    """Any color at all (3/4-bit fallback)."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    if os.environ.get("SIBYL_FORCE_COLOR") == "1":
        return True
    return sys.stdout.isatty()


_TC = supports_truecolor()
_C  = supports_color()
RESET = "\033[0m" if _C else ""


def rgb(r: int, g: int, b: int) -> str:
    """24-bit foreground escape (no-op if color disabled)."""
    if not _TC:
        return ""
    return f"\033[38;2;{r};{g};{b}m"


def rgb_bg(r: int, g: int, b: int) -> str:
    if not _TC:
        return ""
    return f"\033[48;2;{r};{g};{b}m"


def color(text: str, c: tuple[int, int, int]) -> str:
    if not _TC:
        return text
    return f"{rgb(*c)}{text}{RESET}"


# ─── Gradient · char-by-char RGB interpolation ────────────────────────

def _interp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def gradient(text: str, *stops: tuple[int, int, int]) -> str:
    """Color a string with a gradient across N stops, one char at a time.

    Plain-text fallback: returns the input unchanged when color is off.
    Whitespace is preserved (uncolored to keep terminals consistent).
    """
    if not _TC or len(stops) < 2 or not text:
        return text
    out = []
    chars = list(text)
    # Distribute char index across stop segments
    n = max(1, len(chars) - 1)
    segs = len(stops) - 1
    for i, ch in enumerate(chars):
        if ch == " ":
            out.append(ch)
            continue
        seg_f = (i / n) * segs
        seg_i = min(int(seg_f), segs - 1)
        t = seg_f - seg_i
        a = stops[seg_i]
        b = stops[seg_i + 1]
        r = _interp(a[0], b[0], t)
        g = _interp(a[1], b[1], t)
        bb = _interp(a[2], b[2], t)
        out.append(f"\033[38;2;{r};{g};{bb}m{ch}")
    return "".join(out) + RESET


def gradient_gold(text: str) -> str:
    """Pale-gold → deep-ochre flow. The brand's headline gradient."""
    return gradient(text, ACCENT_PALE, ACCENT_GOLD, ACCENT)


def gradient_jade(text: str) -> str:
    """Pulse → jade. Used for success states + live indicators."""
    return gradient(text, PULSE, JADE)


# ─── Style primitives ─────────────────────────────────────────────────

def dim(s: str) -> str:
    return color(s, INK_FAINT)


def muted(s: str) -> str:
    return color(s, INK_MUTE)


def soft(s: str) -> str:
    return color(s, INK_SOFT)


def ink(s: str) -> str:
    return color(s, INK)


def ok(s: str) -> str:
    return color(s, PULSE)


def warn(s: str) -> str:
    return color(s, ACCENT_WARM)


def err(s: str) -> str:
    return color(s, ERROR)


def accent(s: str) -> str:
    return color(s, ACCENT)


def bold(s: str) -> str:
    if not _C:
        return s
    return f"\033[1m{s}{RESET}"


# ─── Composite primitives ─────────────────────────────────────────────

def eyebrow(label: str) -> str:
    """Uppercase letter-spaced ochre label. Editorial section marker."""
    spaced = " ".join(label.upper())
    return color(spaced, ACCENT)


def divider(width: int = 60, *, glyph: str = "─") -> str:
    """Creme-paper rule line."""
    return color(glyph * width, RULE)


def section_header(name: str, *, subtitle: str | None = None, width: int = 60) -> str:
    """The standard subcommand opener.

       ─ <name> ────────────────────────────────────────
       <subtitle, dim>
    """
    name_part = f" {gradient_gold(name)} "
    # Stripped-color length for visible width calc
    visible_name_len = len(f" {name} ")
    rule_left = "─"
    rule_right = "─" * max(3, width - 1 - visible_name_len)
    head = color(rule_left, RULE) + name_part + color(rule_right, RULE)
    if subtitle:
        return head + "\n" + dim(subtitle)
    return head


def chip(text: str, *, palette: str = "accent") -> str:
    """Compact inline label · [text]."""
    palettes = {
        "accent": ACCENT,
        "jade":   PULSE,
        "warn":   ACCENT_WARM,
        "error":  ERROR,
        "mute":   INK_MUTE,
    }
    c = palettes.get(palette, ACCENT)
    return color(f"[{text}]", c)


def kv(label: str, value: str, *, label_width: int = 16, value_color: str = "ink") -> str:
    """One left-aligned label / value row.

    Used across status / whoami / devices for the LOCAL / SERVER blocks.
    """
    palettes = {
        "ink": INK, "soft": INK_SOFT, "mute": INK_MUTE, "faint": INK_FAINT,
        "accent": ACCENT, "ok": PULSE, "warn": ACCENT_WARM, "err": ERROR,
    }
    val_color = palettes.get(value_color, INK_SOFT)
    return f"  {color(label.ljust(label_width), INK_FAINT)} {color(value, val_color)}"


def block_title(text: str) -> str:
    """Sub-section title within a command output. Like 'LOCAL' or 'SERVER'."""
    return "\n" + eyebrow(text)


def success_line(text: str) -> str:
    """Single-line success marker with gradient + glyph."""
    return f"  {ok(GLYPH_OK)} {gradient_jade(text)}"


def warn_line(text: str) -> str:
    return f"  {warn(GLYPH_WARN)} {warn(text)}"


def err_line(text: str) -> str:
    return f"  {err(GLYPH_ERR)} {err(text)}"


def hr_caption(caption: str, *, width: int = 60) -> str:
    """Caption line under a divider — small, muted, centered."""
    pad = max(0, (width - len(caption)) // 2)
    return " " * pad + dim(caption)


def footer_credits(*, width: int = 60) -> str:
    """Bottom-of-output line. Used at end of long outputs."""
    return color("─" * width, RULE) + "\n" + dim("  sibyl labs · memory you can hold in your hand")
