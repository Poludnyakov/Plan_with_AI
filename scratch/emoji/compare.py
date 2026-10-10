from PIL import Image
import sys
names = sys.argv[1].split(",")
originals = ["5348398979866471633", "5348297914991028221", "5348369512095851242"]
files = [f"{c}.webp" for c in originals] + [f"out/{n}.webp" for n in names]
rows = []
for bg in [(255, 255, 255, 255), (23, 33, 43, 255), (14, 22, 33, 255)]:
    row = Image.new("RGBA", (len(files) * 210, 230), bg)
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGBA")
        row.alpha_composite(im.resize((200, 200), Image.LANCZOS), (i * 210 + 5, 5))
        row.alpha_composite(im.resize((24, 24), Image.LANCZOS), (i * 210 + 90, 205))
    rows.append(row)
sheet = Image.new("RGBA", (rows[0].width, 230 * len(rows)))
for i, r in enumerate(rows):
    sheet.alpha_composite(r, (0, i * 230))
sheet.save(sys.argv[2])
