"""Ground truth of a MIREA sheet: (weekday, start, week, title) for a group column."""
import re, sys
DAYS = {"ПОН": 0, "ВТО": 1, "СРЕ": 2, "ЧЕТ": 3, "ПЯТ": 4, "СУБ": 5}

def truth(grid_values, merges, group_col):
    def val(r, c):
        for (r1, c1, r2, c2) in merges:
            if r1 <= r <= r2 and c1 <= c <= c2:
                return grid_values.get((r1, c1))
        return grid_values.get((r, c))
    rows = sorted({r for r, _ in grid_values})
    out, day = [], None
    for r in range(4, max(rows) + 1):
        d = grid_values.get((r, 1))
        if d and str(d)[:3].upper() in DAYS:
            day = DAYS[str(d)[:3].upper()]
        cell = grid_values.get((r, group_col))
        if day is None or not cell or not str(cell).strip():
            continue
        start = str(val(r, 3) or "").replace("-", ":")
        week = {"I": "odd", "II": "even"}.get(str(val(r, 5) or "").strip(), "every")
        for part in re.split(r"\n(?=\s*(?:кр\.?\s*)?\d)", str(cell).strip()):
            out.append((day, start.zfill(5), week, re.sub(r"\s+", " ", part).strip()))
    return out
