"""
Resize and center-crop an input image to 1280x670 (note eyecatch size).

Usage:
        python tools/resize_to_eyecatch.py \
            --in "C:/Users/stair/Downloads/Generated Image 10-18-13-29.png"

Output will be saved to `docs/eyecatch_final/<basename>_1280x670.png` by default.
"""

from __future__ import annotations

import argparse
import os

from PIL import Image


def detect_content_center(image: Image.Image) -> tuple[int, int]:
    """Find center of non-near-white content in the image.

    Returns (cx, cy) in image coordinates.
    """
    rgb = image.convert("RGB")
    px = rgb.load()
    W, H = rgb.size
    minx, miny, maxx, maxy = W, H, 0, 0
    threshold = 245
    found = False
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            if r < threshold or g < threshold or b < threshold:
                found = True
                if x < minx:
                    minx = x
                if y < miny:
                    miny = y
                if x > maxx:
                    maxx = x
                if y > maxy:
                    maxy = y
    if not found:
        return W // 2, H // 2
    return (minx + maxx) // 2, (miny + maxy) // 2


def resize_and_crop(
    in_path: str, out_path: str, width: int = 1280, height: int = 670
) -> str:
    if not os.path.exists(in_path):
        raise FileNotFoundError(in_path)

    img = Image.open(in_path).convert("RGBA")
    iw, ih = img.size

    def resize_cover(image: Image.Image) -> Image.Image:
        # scale to cover
        scale = max(width / iw, height / ih)
        new_w = int(iw * scale + 0.5)
        new_h = int(ih * scale + 0.5)
        return image.resize((new_w, new_h), Image.Resampling.LANCZOS)

    def resize_contain(image: Image.Image) -> Image.Image:
        # scale to contain (fit) and pad later
        scale = min(width / iw, height / ih)
        new_w = int(iw * scale + 0.5)
        new_h = int(ih * scale + 0.5)
        return image.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # detect_content_center is defined at top-level and used by smart mode

    # Default behavior: cover + center crop
    img_cover = resize_cover(img)
    new_w, new_h = img_cover.size

    # center crop default values
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    right = left + width
    bottom = top + height
    img_cropped = img_cover.crop((left, top, right, bottom))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img_cropped.convert("RGB").save(out_path, quality=95)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Resize and center-crop to 1280x670")
    parser.add_argument("--in", dest="in_path", required=True, help="Input image path")
    parser.add_argument(
        "--out",
        dest="out",
        required=False,
        help="Output path (defaults to docs/eyecatch_final/<basename>_1280x670.png)",
    )
    parser.add_argument(
        "--mode",
        dest="mode",
        choices=["cover", "contain", "smart"],
        default="cover",
        help=(
            "cover: fill and crop (default); contain: fit with padding; smart: detect content and center crop"
        ),
    )
    args = parser.parse_args()

    in_path = args.in_path
    out_path = args.out
    if not out_path:
        base = os.path.splitext(os.path.basename(in_path))[0]
        out_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "docs", "eyecatch_final")
        )
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, base + "_1280x670.png")

    try:
        mode = args.mode
        if mode == "cover":
            saved = resize_and_crop(in_path, out_path)
        elif mode == "contain":
            # contain: fit image and pad to target size
            img = Image.open(in_path).convert("RGBA")
            iw, ih = img.size
            scale = min(1280 / iw, 670 / ih)
            new_w = int(iw * scale + 0.5)
            new_h = int(ih * scale + 0.5)
            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            out_img = Image.new("RGB", (1280, 670), (255, 255, 255))
            paste_x = (1280 - new_w) // 2
            paste_y = (670 - new_h) // 2
            out_img.paste(img_resized.convert("RGB"), (paste_x, paste_y))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            out_img.save(out_path, quality=95)
            saved = out_path
        else:  # smart
            img = Image.open(in_path).convert("RGBA")
            # detect content center on original image and map to cover-scaled image
            iw, ih = img.size
            # cover-resize
            scale = max(1280 / iw, 670 / ih)
            new_w = int(iw * scale + 0.5)
            new_h = int(ih * scale + 0.5)
            img_cover = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            # detect center in original coords (top-level function)
            cx, cy = detect_content_center(img)
            # map to resized coords
            cx_r = int(cx * scale)
            cy_r = int(cy * scale)
            # compute crop box centered on (cx_r, cy_r)
            left = max(0, min(new_w - 1280, cx_r - 1280 // 2))
            top = max(0, min(new_h - 670, cy_r - 670 // 2))
            right = left + 1280
            bottom = top + 670
            cropped = img_cover.crop((left, top, right, bottom))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            cropped.convert("RGB").save(out_path, quality=95)
            saved = out_path
        print(f"Saved resized eyecatch: {saved}")
    except FileNotFoundError:
        print(f"Input not found: {in_path}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
