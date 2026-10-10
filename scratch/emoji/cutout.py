"""White background → transparent, cropped, 100×100 WEBP like the original set."""
import pathlib, sys
from collections import deque
from PIL import Image, ImageFilter

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "out"
EMOJI = {
    "check": "✅", "warning": "⚠️", "trash": "🗑", "calendar_sync": "📆", "microphone": "🎙",
    "books": "📚", "history": "📜", "hourglass": "⏳", "clock": "🕒", "location": "📍",
    "search": "🔎", "picture": "🖼", "pencil": "✏️", "undo": "↩️", "refresh": "♻️",
    "card": "💳", "left": "⬅️", "right": "➡️",
}


def whiteness(p):
    r, g, b = p[:3]
    return 255 - min(r, g, b) + (max(r, g, b) - min(r, g, b))  # 0 = pure white


def cut(path: pathlib.Path) -> Image.Image:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    bg = bytearray(w * h)
    queue = deque()
    for x in range(w):
        queue.extend([(x, 0), (x, h - 1)])
    for y in range(h):
        queue.extend([(0, y), (w - 1, y)])
    while queue:
        x, y = queue.popleft()
        i = y * w + x
        if bg[i] or whiteness(px[x, y]) > 38:
            continue
        bg[i] = 1
        if x > 0: queue.append((x - 1, y))
        if x < w - 1: queue.append((x + 1, y))
        if y > 0: queue.append((x, y - 1))
        if y < h - 1: queue.append((x, y + 1))
    mask = Image.frombytes("L", (w, h), bytes(0 if v else 255 for v in bg))
    # Soft edge: alpha from distance to white near the boundary, then a light blur.
    edge = mask.filter(ImageFilter.MaxFilter(5)).point(lambda v: 255 if v else 0)
    near = Image.eval(mask.filter(ImageFilter.MinFilter(5)), lambda v: 255 - v)
    rgba = im.convert("RGBA")
    data = rgba.load(); m = mask.load(); n = near.load()
    for y in range(h):
        for x in range(w):
            if m[x, y] and n[x, y]:  # foreground pixel near the background
                a = max(0.0, min(1.0, whiteness(px[x, y]) / 90))
                r, g, b = px[x, y]
                if a > 0.02:
                    r, g, b = (max(0, min(255, int((c - (1 - a) * 255) / a))) for c in (r, g, b))
                data[x, y] = (r, g, b, int(a * 255))
            elif not m[x, y]:
                data[x, y] = (255, 255, 255, 0)
    alpha = rgba.getchannel("A").filter(ImageFilter.GaussianBlur(0.7))
    rgba.putalpha(Image.eval(alpha, lambda v: 0 if v < 8 else v))
    box = rgba.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    rgba = rgba.crop(box)
    side = max(rgba.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(rgba, ((side - rgba.width) // 2, (side - rgba.height) // 2))
    return square


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True); (OUT / "512").mkdir(exist_ok=True)
    names = sys.argv[1:] or list(EMOJI)
    for name in names:
        square = cut(HERE / "raw" / f"{name}.png")
        square.resize((512, 512), Image.LANCZOS).save(OUT / "512" / f"{name}.png")
        square.resize((100, 100), Image.LANCZOS).save(OUT / f"{name}.webp", "WEBP", quality=92, method=6)
        print(name, (OUT / f"{name}.webp").stat().st_size)
