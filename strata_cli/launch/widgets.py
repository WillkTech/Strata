"""The look of the launch screens: their rules, headings and key hints, on top of the shared list widget.

The palette, `Menu`, `Item` and `Glyphs` live in `strata_cli/tui.py`, so the thin monitor bundle
can draw the model-switcher modal without shipping the launch screens. This module adds the
launch screens' CSS and their small text helpers.
"""

from __future__ import annotations

from rich.text import Text

# Re-exported: the launch screens import the palette, list and glyphs from here.
from strata_cli.tui import (ACCENT, BAD, BG, DARK, FG, LIGHT, MUTE, PANEL, RULE, SEL, WARN,  # noqa: F401
                            Cell, Glyphs, Item, Menu, Palette, cell, unicode_ok)


CSS = f"""
Screen {{ background: {BG}; color: {FG}; }}
#shell {{ width: 100%; height: 100%; background: {PANEL}; padding: 0 1; }}
#head {{ height: 1; }}
#rule1, #rule2 {{ height: 1; color: {RULE}; }}
#body {{ height: 1fr; overflow-y: auto; }}
#explain {{ height: auto; max-height: 4; color: {MUTE}; }}
#hints {{ height: auto; max-height: 3; color: {MUTE}; }}
#message {{ height: auto; max-height: 3; color: {MUTE}; }}
Menu {{ height: auto; max-height: 100%; }}
.field {{ height: 3; border: solid {ACCENT}; display: none; }}
.field.slim {{ height: 1; border: none; padding: 0 1; background: {SEL}; }}
#cmdbox {{ height: 6; border-top: solid {RULE}; scrollbar-size-vertical: 1; }}
.field.open {{ display: block; }}
Input {{ background: {PANEL}; color: {FG}; }}
"""


def heading(text: str) -> Text:
    return Text(text, style=f"bold {ACCENT}")


def muted(text: str) -> Text:
    return Text(text, style=MUTE)


def warn(text: str) -> Text:
    return Text(text, style=WARN)


def bad(text: str) -> Text:
    return Text(text, style=BAD)


def bar(fraction: float, width: int, g: Glyphs) -> str:
    fraction = max(0.0, min(1.0, fraction))
    full = int(round(fraction * width))
    return g.full * full + g.empty * (width - full)
