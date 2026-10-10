"""Generate brand custom emoji in the style of the «planwithai» set via OpenRouter."""
import base64, io, json, sys, pathlib, urllib.request
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
env = dict(l.strip().split("=", 1) for l in open(ROOT / ".env") if "=" in l and not l.startswith("#"))
KEY = env["OPENROUTER_API_KEY"]
MODEL = sys.argv[2] if len(sys.argv) > 2 else "google/gemini-3.1-flash-image"
REFS = ["5348398979866471633", "5348297914991028221", "5348160540462071767", "5350281506981979121"]

SUBJECTS = {
    "check": "a big glossy green rounded check mark ✅ (green square badge with a white tick), held up proudly, happy smile",
    "warning": "a glossy yellow-orange warning triangle with a dark exclamation mark ⚠️, slightly worried but calm face",
    "trash": "a glossy red office waste bin 🗑 with vertical ribs and a domed lid with a handle, clearly a trash can, holding it in front of the hoodie",
    "calendar_sync": "a white desk calendar page with a blue top binding and two small circular sync arrows on it 📆",
    "microphone": "a glossy retro studio microphone 🎙 (silver head, dark stand), singing pose, mouth open",
    "books": "a small stack of three colorful school books (blue, yellow, red) 📚, hugging them",
    "history": "an old parchment scroll partly unrolled with a few lines on it 📜",
    "hourglass": "a golden hourglass with blue sand ⏳, patient waiting face",
    "clock": "a round white wall clock with a yellow rim showing three o'clock 🕒 (different from an alarm clock: no bells)",
    "location": "a glossy red map location pin 📍 on top of a small folded map",
    "search": "a big magnifying glass with a gold rim 🔎, looking through it with one curious eye",
    "picture": "a framed picture with mountains and a sun 🖼, showing it",
    "pencil": "a big yellow pencil ✏️, writing in the air",
    "undo": "a big glossy blue curved arrow pointing back to the left ↩️",
    "refresh": "a glossy green recycling / refresh symbol made of three arrows ♻️",
    "card": "a glossy blue bank card with a gold chip 💳",
    "left": "pointing to the left with the trunk, next to a big glossy blue left arrow ⬅️",
    "hero": "waving hello with one hand and holding a big glossy smartphone in the other hand; the phone screen shows a simple blue calendar with a few colored event blocks and a small chat bubble; joyful welcoming smile. Slightly larger, more detailed hero illustration, same style",
    "laptop": "sitting behind a small glossy silver laptop whose screen shows a weekly calendar grid with colored blocks and NO letters or text at all, typing happily; a few floating 3D icons around (a yellow bell, a red pin, a green check)",
    "wave_hd": "no object: waving hello with one raised hand, other hand relaxed, big friendly open-mouth smile, two small motion lines near the waving hand",
    "bell_hd": "a glossy golden-yellow bell 🔔 with a dark clapper and two small yellow sound waves, ringing it",
    "calendar_hd": "a white rounded desk calendar 📅 with two red rings on top and a grid of light-blue squares, holding it in front of the chest",
    "link_hd": "a big glossy light-blue chain link 🔗 made of two interlocked rounded links, holding it",
    "right": "pointing to the right with the trunk, next to a big glossy BRIGHT LIGHT-BLUE (sky blue #3B8BFF, much lighter than the navy hoodie) right arrow ➡️",
}

PROMPT = (
    "These images are custom emoji of one mascot: a cute blue 3D baby elephant with big pink ears, "
    "rosy cheeks and a dark navy hoodie, shown from the chest up, holding an object. Create ONE new "
    "emoji of exactly the same character, in exactly the same 3D render style, lighting, colors, "
    "proportions, camera angle and framing (character on the left, object in front on the right, "
    "nothing cropped). New object: {subject}. Keep it simple and readable at 24 pixels. "
    "Square image, pure flat white #FFFFFF background, no shadow on the background, no text, no border."
)


def ref_part(cid):
    im = Image.open(HERE / f"{cid}.webp").convert("RGBA").resize((400, 400), Image.LANCZOS)
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255)); bg.alpha_composite(im)
    buf = io.BytesIO(); bg.convert("RGB").save(buf, "PNG")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}}


def generate(name):
    body = {
        "model": MODEL,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": [*(ref_part(c) for c in REFS),
                                                   {"type": "text", "text": PROMPT.format(subject=SUBJECTS[name])}]}],
    }
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=240))
    message = data["choices"][0]["message"]
    images = message.get("images") or []
    if not images:
        raise RuntimeError(f"no image for {name}: {str(message.get('content'))[:200]}")
    url = images[0]["image_url"]["url"]
    raw = base64.b64decode(url.split(",", 1)[1])
    (HERE / "raw" / f"{name}.png").write_bytes(raw)
    print(name, "ok", len(raw), data.get("usage", {}).get("cost"))


if __name__ == "__main__":
    names = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] != "all" else list(SUBJECTS)
    for name in names:
        try:
            generate(name)
        except Exception as error:
            print(name, "ERROR", error)
