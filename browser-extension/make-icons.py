#!/usr/bin/env python3
"""make-icons.py — generate AFFiNE Capture extension icons (v0.3).

Design philosophy: see ``icons/DESIGN.md`` (Focal Threshold).

Glyph:
  - rounded-square field in AFFiNE blue (gradient at >=32px, flat at 16px)
  - four white L-brackets framing the interior
  - a single warm-amber dot at the optical center

Pipeline:
  - render each target size at 4x supersampling
  - downsample with LANCZOS for clean anti-aliasing
  - 16px gets slightly thicker brackets and a flat field for legibility

Usage:
  python make-icons.py            # writes icons/icon-{16,32,48,128}.png

Requires: Pillow.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).parent / "icons"
SIZES = (16, 32, 48, 128)

# Palette — derived from lib/design-tokens.css
BLUE_TOP = (43, 133, 255)        # #2B85FF — AFFiNE blue
BLUE_BOTTOM = (32, 117, 235)     # vertical-gradient terminus (whisper-soft)
WHITE = (255, 255, 255)
GOLD = (255, 203, 71)            # #FFCB47 — warm amber, the focal mark


def _vertical_gradient(size, top, bottom):
    """Return an RGB image of size×size with a vertical gradient."""
    strip = Image.new("RGB", (1, size))
    px = strip.load()
    for y in range(size):
        t = y / (size - 1) if size > 1 else 0
        px[0, y] = (
            round(top[0] * (1 - t) + bottom[0] * t),
            round(top[1] * (1 - t) + bottom[1] * t),
            round(top[2] * (1 - t) + bottom[2] * t),
        )
    return strip.resize((size, size))


def render(size):
    """Render one icon at the given target size (returns PIL.Image)."""
    is_small = size <= 16
    s = size * 4  # supersample
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # 1. Rounded-square mask
    radius = int(s * (0.20 if is_small else 0.22))
    bg_mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(bg_mask).rounded_rectangle(
        (0, 0, s - 1, s - 1), radius=radius, fill=255
    )

    # 2. Background field (flat at small sizes, gradient otherwise)
    if is_small:
        field = Image.new("RGBA", (s, s), BLUE_TOP + (255,))
    else:
        field = _vertical_gradient(s, BLUE_TOP, BLUE_BOTTOM).convert("RGBA")
    field.putalpha(bg_mask)
    img = Image.alpha_composite(img, field)

    # 3. Soft top-edge highlight (large sizes only — invisible at <=32 anyway)
    if size >= 48:
        hl = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(hl).rounded_rectangle(
            (
                int(s * 0.06),
                int(s * 0.06),
                s - int(s * 0.06),
                int(s * 0.50),
            ),
            radius=int(s * 0.16),
            fill=(255, 255, 255, 22),
        )
        hl = hl.filter(ImageFilter.GaussianBlur(s * 0.05))
        clipped = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        clipped.paste(hl, (0, 0), bg_mask)
        img = Image.alpha_composite(img, clipped)

    # 4. Four corner brackets
    draw = ImageDraw.Draw(img)
    if is_small:
        arm = int(s * 0.22)
        thick = max(3, int(s * 0.10))
        margin = int(s * 0.20)
    else:
        arm = int(s * 0.20)
        thick = max(2, int(s * 0.07))
        margin = int(s * 0.22)

    inset = margin
    far = s - margin

    brackets = [
        # each L = three points: arm-end → corner → arm-end
        [(inset, inset + arm), (inset, inset), (inset + arm, inset)],
        [(far - arm, inset), (far, inset), (far, inset + arm)],
        [(inset, far - arm), (inset, far), (inset + arm, far)],
        [(far - arm, far), (far, far), (far, far - arm)],
    ]
    for points in brackets:
        draw.line(points, fill=WHITE, width=thick, joint="curve")

    # Round the bracket tips (PIL doesn't draw round caps on .line)
    cap_r = thick // 2
    if cap_r > 0:
        cap_points = [pt for L in brackets for pt in (L[0], L[2])]
        for cx, cy in cap_points:
            draw.ellipse(
                (cx - cap_r, cy - cap_r, cx + cap_r, cy + cap_r), fill=WHITE
            )

    # 5. Center dot (optical center == geometric center for symmetric glyph)
    dot_r = int(s * (0.13 if is_small else 0.11))
    cx, cy = s // 2, s // 2
    draw.ellipse(
        (cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=GOLD
    )

    # 6. Downsample
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT_DIR.mkdir(exist_ok=True)
    for size in SIZES:
        icon = render(size)
        path = OUT_DIR / f"icon-{size}.png"
        icon.save(path, "PNG", optimize=True)
        print(f"  wrote {path.relative_to(Path(__file__).parent)} ({size}x{size})")


if __name__ == "__main__":
    main()
