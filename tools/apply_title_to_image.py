"""
Apply a title text onto an existing image and save to docs/eyecatch_final/

Usage:
  python tools/apply_title_to_image.py --in "C:/Users/stair/Downloads/Generated Image October 18, 2025 - 12_30PM.png" \
    --title "対話で学ぶ Playwright の CI 失敗を 30 分で潰す実践ガイド"

This script is intentionally small and defensive: it checks the input exists and
creates the output dir if necessary. It tries a few Windows fonts and falls back
to PIL's default font when needed.
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw, ImageFont


def get_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        r"C:/Windows/Fonts/YuGothB.ttc",
        r"C:/Windows/Fonts/meiryob.ttc",
        r"C:/Windows/Fonts/arialbd.ttf",
    ]
    for fp in candidates:
        try:
            if os.path.exists(fp):
                return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default()


def overlay_title(
    in_path: str,
    title: str,
    out_path: str,
    position: str = "top",
    title_only: bool = False,
) -> str:
    if not os.path.exists(in_path):
        raise FileNotFoundError(in_path)

    img = Image.open(in_path).convert("RGBA")
    w, h = img.size

    draw = ImageDraw.Draw(img)

    # Compute font size proportional to width
    font_size = max(28, int(w * 0.06))
    font = get_font(font_size)

    # Wrap title into up to 2 lines
    # Simple heuristic: split at nearest space near half width
    if len(title) > 40:
        # try to split into two roughly equal parts
        words = title.split()
        half = len(words) // 2
        line1 = " ".join(words[:half])
        line2 = " ".join(words[half:])
        lines = [line1, line2]
    else:
        lines = [title]

    # Background band for readability
    padding = int(font_size * 0.6)
    band_height = padding * len(lines) + int(font_size * len(lines))

    # If title_only requested, create a transparent PNG containing only the
    # band + text. Use the same width as the source image for convenience.
    if title_only:
        out_img_h = band_height + int(padding * 1)
        out_img = Image.new("RGBA", (w, out_img_h), (0, 0, 0, 0))
        band = Image.new("RGBA", (w, band_height), (0, 0, 0, 120))
        out_img.paste(band, (0, 0), band)
        draw_out = ImageDraw.Draw(out_img)
        y = padding // 2
        for line in lines:
            bbox = draw_out.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            tx = (w - tw) // 2
            draw_out.text((tx + 2, y + 2), line, font=font, fill=(0, 0, 0, 180))
            draw_out.text((tx, y), line, font=font, fill=(255, 255, 255, 255))
            y += int(font_size * 1.1)

        # Save as PNG (preserve alpha)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out_img.save(out_path, format="PNG")
        return out_path

    # Normal mode: paste semi-transparent band onto original image at top or bottom
    band = Image.new("RGBA", (w, band_height), (0, 0, 0, 120))
    y_top = int(h * 0.06)
    y_bottom = h - band_height - int(h * 0.04)

    if position == "bottom":
        y0 = y_bottom
    else:
        y0 = y_top

    # Paste band
    img.paste(band, (0, y0), band)

    # Draw text on top of pasted band
    y = y0 + padding // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        tx = (w - tw) // 2
        draw.text((tx + 2, y + 2), line, font=font, fill=(0, 0, 0, 180))
        draw.text((tx, y), line, font=font, fill=(255, 255, 255, 255))
        y += int(font_size * 1.1)

    # Ensure output dir exists
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.convert("RGB").save(out_path, quality=95)
    return out_path


def main():
    p = argparse.ArgumentParser(
        description=("Overlay title text onto an image for note eyecatch")
    )
    p.add_argument("--in", dest="in_path", required=True, help="Input image path")
    p.add_argument("--title", dest="title", required=True, help="Title text to overlay")
    p.add_argument(
        "--out",
        dest="out",
        required=False,
        help="Output file path (defaults to docs/eyecatch_final/<basename>)",
    )
    p.add_argument(
        "--position",
        dest="position",
        choices=["top", "bottom"],
        default="top",
        help="Position to place title band (top or bottom)",
    )
    p.add_argument(
        "--title-only",
        dest="title_only",
        action="store_true",
        help="Generate a transparent PNG that contains only the title band",
    )
    args = p.parse_args()

    in_path = args.in_path
    out_path = args.out
    if not out_path:
        base = os.path.splitext(os.path.basename(in_path))[0]
        out_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "docs", "eyecatch_final")
        )
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, base + "_with_title.png")

    try:
        saved = overlay_title(
            in_path,
            args.title,
            out_path,
            position=args.position,
            title_only=args.title_only,
        )
        print(f"Saved eyecatch with title: {saved}")
    except FileNotFoundError:
        print(f"Input file not found: {in_path}")
        raise


if __name__ == "__main__":
    main()
