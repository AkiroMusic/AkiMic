"""Generate AkiMic icon files from the source artwork.

Source: assets/icon-src.png (Akiro's artwork, square, any size).
Run:    .venv/Scripts/python scripts/make_icon.py
Output: assets/icon.png (256) + assets/icon.ico (16..256).
"""

import os

from PIL import Image

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = Image.open(os.path.join(root, "assets", "icon-src.png")).convert("RGBA")

    w, h = src.size
    side = min(w, h)
    src = src.crop(((w - side) // 2, (h - side) // 2,
                    (w + side) // 2, (h + side) // 2))

    assets = os.path.join(root, "assets")
    png_path = os.path.join(assets, "icon.png")
    ico_path = os.path.join(assets, "icon.ico")
    src.resize((256, 256), Image.LANCZOS).save(png_path)
    src.resize((256, 256), Image.LANCZOS).save(ico_path, sizes=SIZES)
    print("icon written:", png_path, ico_path)


if __name__ == "__main__":
    main()
