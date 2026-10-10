import asyncio, time, sys
import harness
from config import settings
from openrouter_service import chat_completion
from interval_ai_service import INTERVAL_SYSTEM_PROMPT
async def one(reasoning):
    t = time.monotonic()
    try:
        r = await chat_completion(model=settings.OPENROUTER_MODEL_FOR_TEXT, messages=[
            {"role": "system", "content": "Сейчас 2026-10-08T20:00:00+03:00.\n" + INTERVAL_SYSTEM_PROMPT},
            {"role": "user", "content": "завтра с 18 до 16 репетиция"}], response_format={"type": "json_object"}, reasoning=reasoning)
        u = r.usage
        det = getattr(u, "completion_tokens_details", None)
        return f"{time.monotonic()-t:.1f}s out={u.completion_tokens} reasoning={getattr(det,'reasoning_tokens',None)} {r.choices[0].message.content[:120]!r}"
    except Exception as e:
        return f"{time.monotonic()-t:.1f}s ERR {type(e).__name__}"
async def main():
    for reasoning in (None, {"enabled": False}, {"effort": "low", "exclude": True}):
        res = await asyncio.gather(*(one(reasoning) for _ in range(3)))
        print(reasoning, *res, sep="\n   ")
asyncio.run(main())
