# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate the icon for the web frontend's desktop shortcut.

Run with:  py -3.12 scripts/make_web_icon.py

Same construction as `make_icon.py` (the desktop build's icon) — same green
field, same cream page, same gold/ink text lines — plus a small globe badge in
the corner. Deliberately a variant, not a distinct icon: this is the same
tool, not a different one, so it should look like family. But two shortcuts
that are pixel-identical except for their label are exactly the kind of thing
that gets clicked by mistake, so the badge is there to be tellable apart at a
glance in a taskbar or a crowded desktop.
"""

from pathlib import Path

from PIL import Image, ImageDraw

# Same tokens make_icon.py uses (src/artifice_ocr/gui/theme.py).
ACCENT = (47, 125, 69)         # --accent  #2f7d45
ACCENT_DEEP = (31, 90, 49)     # --accent-deep #1f5a31
PAPER = (246, 243, 234)        # --paper   #f6f3ea
PAPER_SHADE = (223, 217, 202)  # fold, slightly darker than paper
INK = (75, 70, 61)             # --ink-soft #4b463d
GOLD = (191, 155, 48)          # --gold    #bf9b30
# Badge colour: the same indigo the Analytics charts use for their third
# series (src/artifice_ocr/gui/theme.py's INDIGO / web app.css's --indigo) —
# reused here rather than invented, so it still reads as part of one palette.
INDIGO = (61, 90, 128)         # --indigo  #3d5a80

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "assets" / "artifice_ocr_web.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

SS = 4  # supersample factor


def draw_icon(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=ACCENT)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius,
                        outline=ACCENT_DEEP, width=max(int(s * 0.012), 1))

    left, right = int(s * 0.26), int(s * 0.74)
    top, bottom = int(s * 0.17), int(s * 0.83)
    fold = int(s * 0.17)

    page = [
        (left, top),
        (right - fold, top),
        (right, top + fold),
        (right, bottom),
        (left, bottom),
    ]
    d.polygon(page, fill=PAPER)
    d.polygon([(right - fold, top), (right, top + fold), (right - fold, top + fold)],
              fill=PAPER_SHADE)

    if size >= 24:
        n_lines = 3 if size < 48 else 4
        margin = int(s * 0.07)
        line_top = top + fold + int(s * 0.06)
        gap = (bottom - line_top - margin) / n_lines
        weight = max(int(s * 0.035), SS)
        for i in range(n_lines):
            y = int(line_top + gap * i)
            end = right - margin - (int(s * 0.16) if i == n_lines - 1 else 0)
            colour = GOLD if i == 0 else INK
            d.rounded_rectangle([left + margin, y, end, y + weight],
                                radius=weight // 2, fill=colour)

    # Globe badge, bottom-right corner: a small filled indigo circle, the
    # simplest glyph that reads as "browser / web" rather than "desktop app".
    # Kept deliberately small — a badge that dominates the tile stops reading
    # as a badge and starts reading as a different icon entirely, and at 16px
    # anything more detailed than a plain dot just turns into a smear (the
    # same lesson make_icon.py already learned about the text lines).
    badge_r = s * 0.19
    cx, cy = s * 0.83, s * 0.83
    bbox = [cx - badge_r, cy - badge_r, cx + badge_r, cy + badge_r]
    d.ellipse(bbox, fill=INDIGO, outline=PAPER, width=max(int(s * 0.013), 1))

    if size >= 32:
        line_w = max(int(badge_r * 0.11), 1)
        d.line([(cx - badge_r * 0.7, cy), (cx + badge_r * 0.7, cy)],
              fill=PAPER, width=line_w)
        merid_bbox = [cx - badge_r * 0.35, cy - badge_r * 0.82,
                     cx + badge_r * 0.35, cy + badge_r * 0.82]
        d.ellipse(merid_bbox, outline=PAPER, width=line_w)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw_icon(n) for n in SIZES]
    frames[-1].save(TARGET, format="ICO",
                    sizes=[(n, n) for n in SIZES],
                    append_images=frames[:-1])
    print(f"Wrote {TARGET}  ({', '.join(f'{n}x{n}' for n in SIZES)})")

    preview = ROOT / "assets" / "artifice_ocr_web_preview.png"
    draw_icon(256).save(preview)
    print(f"Wrote {preview}")


if __name__ == "__main__":
    main()
