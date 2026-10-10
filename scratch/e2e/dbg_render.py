import sys
from pathlib import Path
import harness  # noqa: env
import spreadsheet_schedule_parser as s
path, prompt = sys.argv[1], sys.argv[2]
grids = s._read_xls(Path(path).read_bytes()) if path.endswith(".xls") else s._read_xlsx(Path(path).read_bytes())
sheet = s._prepare_sheet(grids[int(sys.argv[3]) if len(sys.argv) > 3 else 0])
proj, labels = s._project_columns(sheet, prompt)
print("labels", labels, "header", s._header_rows(sheet), "rows", len(proj.rows))
blocks = s._split_rows(proj)
print("chunks", len(blocks))
print(s._render_chunk(s.Chunk(sheet=proj, rows=blocks[0]) if hasattr(s.Chunk, "__dataclass_fields__") else None, len(proj.rows))[:6000])
