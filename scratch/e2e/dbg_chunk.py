import asyncio, sys, json
from pathlib import Path
import harness  # noqa
import spreadsheet_schedule_parser as s
async def main():
    path, prompt, idx = sys.argv[1], sys.argv[2], int(sys.argv[3])
    sheet = s._prepare_sheet(s._read_xlsx(Path(path).read_bytes())[0])
    proj, _ = s._project_columns(sheet, prompt)
    blocks = s._split_rows(proj)
    chunk = s.Chunk(sheet=proj, rows=blocks[idx])
    for c, r in await s._read_chunk(chunk, prompt, len(proj.rows)):
        for e in r.events:
            print(e["cell"], e.get("weekday"), e.get("start"), e.get("week"), e.get("title"))
asyncio.run(main())
