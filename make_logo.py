"""Generate a 480x480 PNG logo for the BUIDL submission.

Plain, no emoji, no stock-art vibe — a stylised "sentinel eye" over a
ledger grid in Mantle's brand orange. Single PIL pass, no external assets.
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUT = Path(__file__).parent / "logo.png"
SIZE = 480
BG = (10, 12, 16)           # near-black
FG = (255, 255, 255)
ACC = (255, 145, 60)        # Mantle orange-ish
DIM = (60, 70, 85)


def draw_grid(d: ImageDraw.ImageDraw):
    """Thin grid behind the eye — ledger / on-chain vibe."""
    step = 24
    for x in range(0, SIZE, step):
        d.line([(x, 0), (x, SIZE)], fill=DIM, width=1)
    for y in range(0, SIZE, step):
        d.line([(0, y), (SIZE, y)], fill=DIM, width=1)


def draw_eye(d: ImageDraw.ImageDraw):
    cx, cy = SIZE // 2, SIZE // 2
    # outer eye almond shape — two arcs
    eye_w, eye_h = 280, 140
    d.ellipse([cx - eye_w // 2, cy - eye_h // 2, cx + eye_w // 2, cy + eye_h // 2],
              outline=FG, width=6)
    # iris
    iris_r = 56
    d.ellipse([cx - iris_r, cy - iris_r, cx + iris_r, cy + iris_r],
              fill=ACC, outline=FG, width=4)
    # pupil
    pupil_r = 22
    d.ellipse([cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r], fill=(10, 12, 16))
    # tiny highlight
    d.ellipse([cx + 6, cy - 18, cx + 18, cy - 6], fill=FG)


def font(size: int):
    for p in (r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\consola.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def main():
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    draw_grid(d)
    draw_eye(d)
    # label
    f = font(32)
    label = "SENTINEL"
    w = d.textlength(label, font=f)
    d.text(((SIZE - w) / 2, 380), label, font=f, fill=ACC)
    img.save(OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
