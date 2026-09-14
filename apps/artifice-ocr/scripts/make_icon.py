# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate the application icon from the project's design tokens.

Run with:  py -3.12 scripts/make_icon.py

Draws a document on an accent-green field. Each size is rendered natively at
4x and downsampled, rather than scaling one large bitmap — a 256px drawing
squeezed to 16px turns to mush, so the small sizes deliberately drop detail.
"""

from pathlib import Path

from PIL import Image, ImageDraw

# From src/artifice_ocr/gui/theme.py (which follows the public_history tokens).
ACCENT = (47, 125, 69)         # --accent  #2f7d45
ACCENT_DEEP = (31, 90, 49)     # --accent-deep #1f5a31
PAPER = (246, 243, 234)        # --paper   #f6f3ea
PAPER_SHADE = (223, 217, 202)  # fold, slightly darker than paper
INK = (75, 70, 61)             # --ink-soft #4b463d
GOLD = (191, 155, 48)          # --gold    #bf9b30

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "assets" / "artifice_ocr.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

SS = 4  # supersample factor


def draw_icon(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Field. A single rounded tile — an earlier version layered a darker band
    # at the foot, which left notches where the two corner radii met.
    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=ACCENT)
    # Hairline inner edge, the icon equivalent of the UI's rules.
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius,
                        outline=ACCENT_DEEP, width=max(int(s * 0.012), 1))

    # Page
    left, right = int(s * 0.26), int(s * 0.74)
    top, bottom = int(s * 0.17), int(s * 0.83)
    fold = int(s * 0.17)  # size of the turned corner

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

    # Text lines — dropped entirely at the smallest sizes, where they would
    # smear into a grey block.
    if size >= 24:
        n_lines = 3 if size < 48 else 4
        margin = int(s * 0.07)
        line_top = top + fold + int(s * 0.06)
        gap = (bottom - line_top - margin) / n_lines
        weight = max(int(s * 0.035), SS)
        for i in range(n_lines):
            y = int(line_top + gap * i)
            # last line short, like a paragraph end
            end = right - margin - (int(s * 0.16) if i == n_lines - 1 else 0)
            colour = GOLD if i == 0 else INK
            d.rounded_rectangle([left + margin, y, end, y + weight],
                                radius=weight // 2, fill=colour)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw_icon(n) for n in SIZES]
    frames[-1].save(TARGET, format="ICO",
                    sizes=[(n, n) for n in SIZES],
                    append_images=frames[:-1])
    print(f"Wrote {TARGET}  ({', '.join(f'{n}x{n}' for n in SIZES)})")

    preview = ROOT / "assets" / "artifice_ocr_preview.png"
    draw_icon(256).save(preview)
    print(f"Wrote {preview}")


if __name__ == "__main__":
    main()
