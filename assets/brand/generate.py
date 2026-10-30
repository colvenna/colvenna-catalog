#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate the self-contained Colvenna Catalog SVG brand assets, and with --png their PNGs.

The mark is the Colvenna series' (three concentric flow lines forming a C, the innermost flowing
into a gold result point), as Colvenna for ClickHouse's brand generator draws it and Colvenna
Lake's, which this script follows, renders it;
the wordmark is "Colvenna" in bold and "Catalog" in regular weight, over the tagline settled in #26
(no Apache project name in it). Run from anywhere: the files are written next to this script, the
same bytes every time. --png renders the PNGs with rsvg-convert, at the sizes PNGS gives.
"""

import math
import subprocess
import sys
from pathlib import Path


OUTPUT = Path(__file__).resolve().parent
FONT = "Arial, Helvetica, sans-serif"
NAME = "Colvenna Catalog"
# The horizontal mark's second line.
TAGLINE = "OPEN REST CATALOG · EKS + SERVERLESS"

# The series mark: (radius, start, end, opacity) of each flow line, and the result point.
FLOWS = ((86, 40, 320, "1"), (62, 52, 330, ".8"), (38, 66, 345, ".6"))
RESULT = (186.7, 114.2)

# The horizontal logo: wide enough for the wordmark (Arial's widths: it ends near x = 621), at 160 px high.
LOGO_WIDTH, LOGO_HEIGHT = 660, 160
PREVIEW_WIDTH, PREVIEW_HEIGHT = 960, 670
# The PNGs: name, width, height.
PNGS = (("logo-light", 2 * LOGO_WIDTH, 2 * LOGO_HEIGHT), ("logo-dark", 2 * LOGO_WIDTH, 2 * LOGO_HEIGHT),
        ("icon", 512, 512), ("brand-preview", 2 * PREVIEW_WIDTH, 2 * PREVIEW_HEIGHT))


def arc(radius, start, end):
    """Clockwise arc around the tile centre, angles in degrees from +x."""
    a0, a1 = math.radians(start), math.radians(end)
    x0, y0 = 128 + radius * math.cos(a0), 128 + radius * math.sin(a0)
    x1, y1 = 128 + radius * math.cos(a1), 128 + radius * math.sin(a1)
    large = 1 if (end - start) % 360 > 180 else 0
    return f"M{x0:.1f} {y0:.1f} A{radius} {radius} 0 {large} 1 {x1:.1f} {y1:.1f}"


def mark_defs(prefix):
    flows = "".join(
        f'\n      <path d="{arc(radius, start, end)}" fill="none" '
        f'stroke="url(#{prefix}-flow)" stroke-opacity="{opacity}" '
        f'stroke-width="15" stroke-linecap="round"/>'
        for radius, start, end, opacity in FLOWS
    )
    return f"""
  <defs>
    <linearGradient id="{prefix}-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#11556B"/>
      <stop offset="1" stop-color="#062533"/>
    </linearGradient>
    <linearGradient id="{prefix}-flow" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="#1BB59E"/>
      <stop offset="1" stop-color="#4FF2D6"/>
    </linearGradient>
    <g id="{prefix}-mark">
      <rect width="256" height="256" rx="56" fill="url(#{prefix}-bg)"/>{flows}
      <circle cx="{RESULT[0]}" cy="{RESULT[1]}" r="12" fill="#F5CB68"/>
    </g>
  </defs>"""


def logo_content(theme, prefix):
    ink, muted = (
        ("#123D4A", "#52707A")
        if theme == "light"
        else ("#EAF7F4", "#A8C9C4")
    )
    return f"""{mark_defs(prefix)}
  <use href="#{prefix}-mark" transform="translate(8 16) scale(.5)"/>
  <text x="162" y="85" fill="{ink}" font-family="{FONT}"
        font-size="59" font-weight="700" letter-spacing="-1.7">Colvenna<tspan
        dx="16" font-weight="400">Catalog</tspan></text>
  <text x="165" y="118" fill="{muted}" font-family="{FONT}"
        font-size="12" font-weight="600" letter-spacing="1.8">{TAGLINE}</text>"""


def svg(width, height, prefix, description, content):
    document = f"""<svg xmlns="http://www.w3.org/2000/svg"
     width="{width}" height="{height}" viewBox="0 0 {width} {height}"
     role="img" aria-labelledby="{prefix}-title {prefix}-desc">
  <title id="{prefix}-title">{NAME}</title>
  <desc id="{prefix}-desc">{description}</desc>
{content}
</svg>
"""
    return "\n".join(line.rstrip() for line in document.splitlines()) + "\n"


def assets():
    """{file name: SVG text} of every asset."""
    files = {}
    for theme in ("light", "dark"):
        prefix = f"colvenna-catalog-{theme}"
        files[f"logo-{theme}.svg"] = svg(
            LOGO_WIDTH,
            LOGO_HEIGHT,
            prefix,
            "Three concentric teal flow lines form a C; the innermost line flows into a gold "
            f"result point. Colvenna Catalog wordmark and tagline for {theme} backgrounds.",
            logo_content(theme, prefix),
        )

    prefix = "colvenna-catalog-icon"
    files["icon.svg"] = svg(
        256,
        256,
        prefix,
        "Three concentric teal flow lines form a C on a deep blue tile; "
        "the innermost line flows into a gold result point.",
        mark_defs(prefix) + f'\n  <use href="#{prefix}-mark"/>',
    )

    # The logos, centred in their cards; the icon at 96, 64, 32 and 24 px.
    x = 32 + (896 - LOGO_WIDTH) // 2
    preview = f"""
  <rect width="{PREVIEW_WIDTH}" height="{PREVIEW_HEIGHT}" fill="#E8F0EE"/>
  <text x="32" y="42" fill="#123D4A" font-family="{FONT}"
        font-size="24" font-weight="700">Colvenna<tspan
        dx="7" font-weight="400">Catalog</tspan></text>
  <text x="32" y="67" fill="#52707A" font-family="{FONT}"
        font-size="12" letter-spacing="1.5">COLUMNS AND FLOW · BRAND ASSETS</text>
  <rect x="32" y="92" width="896" height="184" rx="24" fill="#FAFCFB"/>
  <g transform="translate({x} 104)">
    {logo_content("light", "preview-light")}
  </g>
  <rect x="32" y="294" width="896" height="184" rx="24" fill="#071D29"/>
  <g transform="translate({x} 306)">
    {logo_content("dark", "preview-dark")}
  </g>
  {mark_defs("preview-size")}
  <use href="#preview-size-mark" transform="translate(52 520) scale(.375)"/>
  <use href="#preview-size-mark" transform="translate(210 552) scale(.25)"/>
  <use href="#preview-size-mark" transform="translate(358 584) scale(.125)"/>
  <use href="#preview-size-mark" transform="translate(486 592) scale(.09375)"/>
  <g fill="#52707A" font-family="{FONT}" font-size="12">
    <text x="76" y="646">96 px</text>
    <text x="226" y="646">64 px</text>
    <text x="358" y="646">32 px</text>
    <text x="482" y="646">24 px</text>
  </g>
  <rect x="580" y="518" width="348" height="126" rx="24" fill="#071D29"/>
  <use href="#preview-size-mark" transform="translate(610 549) scale(.25)"/>
  <text x="698" y="573" fill="#EAF7F4" font-family="{FONT}"
        font-size="14" font-weight="700">Columns and flow.</text>
  <text x="698" y="597" fill="#A8C9C4" font-family="{FONT}"
        font-size="12">Every stream reaches a result.</text>"""
    files["brand-preview.svg"] = svg(
        PREVIEW_WIDTH,
        PREVIEW_HEIGHT,
        "colvenna-catalog-preview",
        "Light and dark Colvenna Catalog wordmarks, with application icons at 96, 64, 32 and 24 pixels.",
        preview,
    )
    return files


def write_assets(png=False):
    for name, text in assets().items():
        (OUTPUT / name).write_text(text, encoding="utf-8")
    if png:
        for name, width, height in PNGS:
            subprocess.run(["rsvg-convert", "-w", str(width), "-h", str(height), str(OUTPUT / f"{name}.svg"),
                            "-o", str(OUTPUT / f"{name}.png")], check=True)


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--png"]):
        sys.exit("usage: generate.py [--png]")
    write_assets(png=sys.argv[1:] == ["--png"])
