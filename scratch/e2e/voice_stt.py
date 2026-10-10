import asyncio, time
from pathlib import Path
import harness  # noqa
from openrouter_service import transcribe_audio
HERE = Path(__file__).resolve().parent / "voice"
async def one(name, fmt="ogg"):
    t = time.monotonic()
    try:
        text = await transcribe_audio((HERE / name).read_bytes(), audio_format=fmt)
    except Exception as e:
        text = f"ОШИБКА {type(e).__name__}: {str(e)[:200]}"
    print(f"{name:16} {fmt:4} {time.monotonic()-t:4.1f}s  {text}")
async def main():
    names = sorted(p.name for p in HERE.glob("*.ogg"))
    await asyncio.gather(*(one(n) for n in names))
    await one("v02.m4a", "ogg"); await one("v02.mp3", "ogg"); await one("v02.m4a", "m4a"); await one("v02.mp3", "mp3")
asyncio.run(main())
