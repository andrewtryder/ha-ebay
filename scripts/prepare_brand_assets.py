#!/usr/bin/env python3
"""Generate Home Assistant brand assets for the eBay custom integration.

This script downloads the eBay logo from Wikimedia Commons and creates:
- custom_components/ebay/brand/logo.png
- custom_components/ebay/brand/logo@2x.png
- custom_components/ebay/brand/icon.png
- custom_components/ebay/brand/icon@2x.png

Requires:
    python -m pip install pillow

The eBay logo may be trademarked. Use it only for identification and include
the trademark notice in README.md.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import urllib.request

from PIL import Image


SOURCE_URL = "https://upload.wikimedia.org/wikipedia/commons/4/48/EBay_logo.png"
BRAND_DIR = Path("custom_components/ebay/brand")


def _download_source() -> Image.Image:
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "ha-ebay-brand-assets/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    image = Image.open(BytesIO(data)).convert("RGBA")
    return _trim_transparent(image)


def _trim_transparent(image: Image.Image) -> Image.Image:
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return image
    return image.crop(bbox)


def _fit_width(image: Image.Image, width: int) -> Image.Image:
    ratio = width / image.width
    height = round(image.height * ratio)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _fit_height(image: Image.Image, height: int) -> Image.Image:
    ratio = height / image.height
    width = round(image.width * ratio)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _make_logo(source: Image.Image, height: int) -> Image.Image:
    return _fit_height(source, height)


def _make_icon(source: Image.Image, size: int) -> Image.Image:
    # Keep the full eBay wordmark inside a square transparent canvas.
    # Use 90% width to avoid clipping in rounded UI masks.
    fitted = _fit_width(source, round(size * 0.90))
    if fitted.height > round(size * 0.70):
        fitted = _fit_height(source, round(size * 0.70))

    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    canvas.alpha_composite(fitted, (x, y))
    return canvas


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)

    source = _download_source()

    assets = {
        "logo.png": _make_logo(source, 256),
        "logo@2x.png": _make_logo(source, 512),
        "icon.png": _make_icon(source, 256),
        "icon@2x.png": _make_icon(source, 512),
    }

    for filename, image in assets.items():
        path = BRAND_DIR / filename
        image.save(path, "PNG", optimize=True)
        print(f"Wrote {path} ({image.width}x{image.height})")


if __name__ == "__main__":
    main()
