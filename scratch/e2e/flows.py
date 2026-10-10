"""Multi-step dialogues: conflicts, edits, deletes, xlsx imports."""
import asyncio, json, sys, time
from datetime import date
from harness import TgSim, user_events, user_series, show
from database import init_db
from pathlib import Path
HERE = Path(__file__).resolve().parent

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"   {'✔' if cond else '✘'} {name}{(' — ' + detail) if detail and not cond else ''}")


def last_card(sim):
    return max(mid for mid, m in sim.markups.items() if any((b.callback_data or "").startswith("confirm:") for row in m.inline_keyboard for b in row))


async def say(sim, text):
    print(f"\n» {text}")
    out = await sim.text(text)
    show(out)
    return out


async def press(sim, label, mid=None):
    out, answers = await sim.press(label, mid)
    print(f"  [нажата «{label}»] ответ: {answers}")
    show(out)
    return out, answers


def joined(out):
    return "\n".join(t or "" for _, _, t, _ in out)


async def conflicts():
    print("\n=== Пересечения, правки, удаление ===")
    sim = TgSim(user_id=770000000 + int(time.time()) % 100000)
    await say(sim, "/start")
    await say(sim, "завтра с 15 до 17 встреча с инвестором")
    out, ans = await press(sim, "Подтвердить", last_card(sim))
    check("первое событие подтверждено", "Добавлено" in joined(out))

    await say(sim, "завтра в 16:00 созвон с бухгалтером")
    out, ans = await press(sim, "Подтвердить", last_card(sim))
    check("пересечение 16:00 внутри 15–17 обнаружено", "перекрываются" in joined(out), joined(out))

    await say(sim, "завтра с 17 до 18 спортзал")
    out, ans = await press(sim, "Подтвердить", last_card(sim))
    check("соседний интервал 17–18 не считается пересечением", "Добавлено" in joined(out), joined(out))

    await say(sim, "завтра с 14:30 до 15:30 обед")
    out, ans = await press(sim, "Подтвердить", last_card(sim))
    check("частичное пересечение 14:30–15:30 обнаружено", "перекрываются" in joined(out), joined(out))

    await say(sim, "завтра весь день командировка в Тверь")
    out, ans = await press(sim, "Подтвердить", last_card(sim))
    check("событие на весь день добавляется поверх", "Добавлено" in joined(out), joined(out))

    out = await say(sim, "завтра в 20 ужин с коллегами")
    check("карточка предупреждает о событии на весь день", "весь день" in joined(out), joined(out))
    out, ans = await press(sim, "Отменить", last_card(sim))

    out = await say(sim, "перенеси спортзал на 15:30")
    check("перенос в занятое время отклонён", "пересека" in joined(out).lower(), joined(out))
    out = await say(sim, "перенеси спортзал на 19:00")
    events = await user_events(sim.user.id)
    gym = [e for e in events if "спорт" in e[0].lower()]
    check("перенос спортзала на 19:00", gym and gym[0][2].hour == 19, str(gym))

    out = await say(sim, "отмени встречу с инвестором")
    events = await user_events(sim.user.id)
    check("встреча с инвестором удалена", not any("инвест" in e[0].lower() for e in events), str(events))

    out = await say(sim, "завтра в 16 созвон с бухгалтером")
    out, ans = await press(sim, "Подтвердить", last_card(sim))
    check("после удаления слот 16:00 свободен", "Добавлено" in joined(out), joined(out))
    print("\nИтог календаря:")
    for e in await user_events(sim.user.id):
        print("  ", e[0], e[1], f"{e[2]:%d.%m %H:%M}–{e[3]:%d.%m %H:%M}", "весь день" if e[4] else "")


async def xlsx_flows():
    print("\n=== XLSX ===")
    truth = json.load(open(HERE / "xlsx/truth.json"))

    def series_rows(series):
        return sorted((s.valid_from.isoformat() if s.valid_from == s.valid_until else s.weekday,
                       s.start_time.strftime("%H:%M"), s.end_time.strftime("%H:%M"), s.title) for s in series)

    async def import_file(name, caption=None, followups=()):
        sim = TgSim(user_id=760000000 + int(time.time() * 1000) % 1000000)
        print(f"\n» файл {name}" + (f" с подписью {caption!r}" if caption else ""))
        out = await sim.document(HERE / "xlsx" / name, caption)
        show(out)
        for text in followups:
            await say(sim, text)
        rows = series_rows(await user_series(sim.user.id))
        for r in rows:
            print("     ряд:", r)
        return sim, rows

    def covers(rows, expected):
        missing = []
        for day, start, end, title in expected:
            hit = [r for r in rows if r[0] == day and (start is None or r[1] == start)
                   and (end is None or r[2] == end) and title.lower()[:8] in r[3].lower()]
            if not hit:
                missing.append((day, start, end, title))
        return missing

    sim, rows = await import_file("1_list.xlsx")
    miss = covers(rows, truth["1_list.xlsx"])
    check("список с датами: все 6 занятий", not miss and len(rows) == 6, f"нет {miss}, всего {len(rows)}")

    sim, rows = await import_file("2_weekly.xlsx", followups=["с 12 октября по 25 декабря"])
    weekly = [(w, s, e, t) for w, s, e, t in truth["2_weekly.xlsx"]["weekly"]]
    miss = covers(rows, weekly)
    check("недельная сетка + период: 7 занятий", not miss and len(rows) == 7, f"нет {miss}, всего {len(rows)}")

    sim, rows = await import_file("3_two_groups.xlsx")
    sent = [t for _, _, t, _ in sim.log if t]
    check("две группы: бот спрашивает группу", any("групп" in (t or "").lower() for t in sent[-2:]), sent[-1] if sent else "")
    await say(sim, "ИВТ-22")
    rows = series_rows(await user_series(sim.user.id))
    for r in rows:
        print("     ряд:", r)
    miss = covers(rows, truth["3_two_groups.xlsx"]["ИВТ-22"])
    check("две группы: после ответа «ИВТ-22» ровно её 4 пары", not miss and len(rows) == 4, f"нет {miss}, всего {len(rows)}")

    sim, rows = await import_file("3_two_groups.xlsx", caption="группа ИВТ-21")
    miss = covers(rows, truth["3_two_groups.xlsx"]["ИВТ-21"])
    check("две группы с подписью «группа ИВТ-21»: её 4 пары", not miss and len(rows) == 4, f"нет {miss}, всего {len(rows)}")

    sim, rows = await import_file("4_deadlines.xlsx")
    miss = covers(rows, truth["4_deadlines.xlsx"])
    check("дедлайны в свободном формате: 3 события", not miss and len(rows) == 3, f"нет {miss}, всего {len(rows)}")

    sim, rows = await import_file("5_calendar_grid.xlsx")
    miss = covers(rows, truth["5_calendar_grid.xlsx"])
    check("календарная сетка с датами в шапке: 4 события", not miss and len(rows) == 4, f"нет {miss}, всего {len(rows)}")

    # Personal event on top of an imported lesson
    sim, rows = await import_file("1_list.xlsx")
    out = await say(sim, "12 октября в 9:30 встреча с деканом")
    card = joined(out)
    out, ans = await press(sim, "Подтвердить", last_card(sim))
    print("     карточка упоминает пару?", "анализ" in card.lower(), "| подтверждение:", joined(out).replace("\n", " "))
    check("пересечение с импортированной парой видно пользователю", "анализ" in (card + joined(out)).lower(), card)


async def main():
    await init_db()
    which = sys.argv[1:] or ["conflicts", "xlsx"]
    if "conflicts" in which:
        await conflicts()
    if "xlsx" in which:
        await xlsx_flows()
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} проверок прошло")
    for n in bad:
        print("  ✘", n)

asyncio.run(main())
