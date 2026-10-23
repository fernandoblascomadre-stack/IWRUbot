"""Turns Fernando's rocket-vote image (PNG with a transparent background) into the sticker rocket_bot.py posts:
assets/rocket_vote.webp, longest side 512 px (Telegram's sticker size), transparency kept, under Telegram's 512 KB.
Run: python make_rocket_sticker.py <image.png>"""
import io
import os
import sys

from PIL import Image

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "rocket_vote.webp")
MAX_BYTES = 512 * 1024


def make(src, out=OUT):
    img = Image.open(src).convert("RGBA")
    if img.getextrema()[3][0] == 255:
        print("warning: the image has no transparent pixels -- it will show as a square", flush=True)
    img.thumbnail((512, 512), Image.LANCZOS)
    if max(img.size) < 512:  # Telegram wants one side exactly 512
        k = 512 / max(img.size)
        img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
    for quality in (100, 95, 90, 85, 80, 70):
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=quality, lossless=(quality == 100), method=6)
        if buf.tell() <= MAX_BYTES:
            break
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(buf.getvalue())
    print(f"{out}: {img.width}x{img.height}, {buf.tell() // 1024} KB, quality {quality}", flush=True)
    return out


if __name__ == "__main__":
    make(sys.argv[1])
