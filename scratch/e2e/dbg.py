import asyncio, sys
import harness  # env setup
from anonymizer import DataAnonymizer
from interval_ai_service import extract_intervals
a = DataAnonymizer()
async def main():
    for t in sys.argv[1:]:
        an = a.anonymize_text(t)
        print(repr(t), "→ anonymized:", repr(an))
        try:
            ev = await extract_intervals(an)
        except Exception as e:
            ev = e
        print("   extracted:", ev)
        if isinstance(ev, list):
            for e in ev:
                print("   clean title:", repr(a.clean_event_title(e["title"])))
asyncio.run(main())
