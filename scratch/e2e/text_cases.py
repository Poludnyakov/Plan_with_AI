"""Russian free-text inputs → events in the DB. Today is Thu 2026-10-08 (MSK)."""
import asyncio, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from harness import TgSim, user_events, show
from database import init_db
import interval_ai_service


class FrozenDateTime(datetime):
    """The expectations below are written for Thu 2026-10-08 12:00 MSK."""
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 8, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")).astimezone(tz) if tz else cls(2026, 10, 8, 12, 0)


interval_ai_service.datetime = FrozenDateTime

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime.now(MSK)
RUN = int(time.time())
D = lambda m, d, h=0, mi=0, y=2026: datetime(y, m, d, h, mi, tzinfo=MSK)
AD = "allday"
# (text, [ (start, end) | (AD, first_day, last_day_inclusive) ] or None for "no events", alternatives allowed via list of lists)
CASES = [
    ("завтра в 15 встреча с врачом", [(D(10,9,15), D(10,9,16))]),
    ("послезавтра с 10 до 12 лекция по матану", [(D(10,10,10), D(10,10,12))]),
    ("в понедельник в 9:30 созвон с командой", [(D(10,12,9,30), D(10,12,10,30))]),
    ("контрольная по физике 20 октября в 11", [(D(10,20,11), D(10,20,12,30))]),
    ("сдать курсовую до 18:00 в пятницу", [(D(10,9,17,30), D(10,9,18))]),
    ("день рождения мамы 25 октября", [(AD, D(10,25), D(10,25))]),
    ("отпуск с 1 по 7 ноября", [(AD, D(11,1), D(11,7))]),
    ("в эту субботу в 7 вечера кино", [(D(10,10,19), D(10,10,20))]),
    ("завтра в полдень обед с Сашей", [(D(10,9,12), D(10,9,13))]),
    ("в среду в пол третьего стоматолог", [(D(10,14,14,30), D(10,14,15,30))]),
    ("в понедельник без пятнадцати девять планёрка", [(D(10,12,8,45), D(10,12,9,45))]),
    ("завтра в 8 утра пробежка, а в 19:00 тренировка по боксу", [(D(10,9,8), D(10,9,9)), (D(10,9,19), D(10,9,20))]),
    ("24.10 в 14:00 собеседование в офисе на Тверской 12", [(D(10,24,14), D(10,24,15))]),
    ("12 ноября с 18:30 до 21:00 концерт в Крокусе", [(D(11,12,18,30), D(11,12,21))]),
    ("зафтра в 5 вечира встреча с куратором", [(D(10,9,17), D(10,9,18))]),
    ("сегодня в 23:30 созвон с партнёрами до часу ночи", [(D(10,8,23,30), D(10,9,1))]),
    ("в субботу дежурство с 22:00 до 02:00", [(D(10,10,22), D(10,11,2))]),
    ("31 октября в 10:00 субботник", [(D(10,31,10), D(10,31,11))]),
    ("15 числа в 16:00 педсовет", [(D(10,15,16), D(10,15,17))]),
    ("на выходных поездка на дачу", [(AD, D(10,10), D(10,11))]),
    ("завтра весь день конференция", [(AD, D(10,9), D(10,9))]),
    ("завтра в шесть вечера ужин с родителями", [(D(10,9,18), D(10,9,19))]),
    ("ЗАВТРА В 10 ЭКЗАМЕН ПО ИСТОРИИ", [(D(10,9,10), D(10,9,11,30))]),
    ("Встреча 9 октября 2026 года в 13.30", [(D(10,9,13,30), D(10,9,14,30))]),
    ("через неделю в 12 защита диплома", [(D(10,15,12), D(10,15,13,30)), ]),
    ("1 декабря в 9:00 — сдача отчёта, 2 декабря в 9:00 — планирование", [(D(12,1,9), D(12,1,10)), (D(12,2,9), D(12,2,10))]),
    ("в следующую пятницу с 14 до 16 семинар", [[(D(10,16,14), D(10,16,16))], [(D(10,9,14), D(10,9,16))]]),
    ("10 января 2027 в 11 утра вылет в Казань", [(D(1,10,11,y=2027), D(1,10,12,y=2027))]),
    ("привет, как дела?", None),
    ("спасибо!", None),
]


def expected_variants(spec):
    if spec and isinstance(spec[0], list):
        return spec
    return [spec]


def matches(events, spec):
    if spec is None:
        return not events
    if len(events) != len(spec):
        return False
    got = sorted(events, key=lambda e: e[2])
    for ev, want in zip(got, sorted(spec, key=lambda w: w[1] if w[0] == AD else w[0])):
        title, status, start, end, all_day = ev
        if want[0] == AD:
            if not (all_day and start == want[1] and end == want[2] + timedelta(days=1)):
                return False
        else:
            # Start must be exact; end may differ only when the message gave no end (duration defaults).
            if all_day or start != want[0]:
                return False
            if end != want[1] and not (timedelta(minutes=30) <= end - start <= timedelta(hours=3)):
                return False
    return True


async def run_case(i, text, spec, sem, results):
    async with sem:
        sim = TgSim(user_id=800000000 + (RUN % 10000) * 100 + i)
        started = time.monotonic()
        out = await sim.text(text)
        events = await user_events(sim.user.id)
        ok = any(matches(events, v) for v in expected_variants(spec))
        replies = [t for n, _, t, _ in out if n == "SendMessage" and t and not t.startswith("⏳")]
        results[i] = (ok, text, events, replies, time.monotonic() - started)


async def main():
    await init_db()
    only = {int(a) for a in sys.argv[1:]}
    sem = asyncio.Semaphore(6)
    results = {}
    await asyncio.gather(*(run_case(i, t, s, sem, results) for i, (t, s) in enumerate(CASES) if not only or i in only))
    bad = 0
    for i in sorted(results):
        ok, text, events, replies, took = results[i]
        bad += not ok
        print(f"{'OK ' if ok else 'BAD'} #{i} {text!r} ({took:.1f}s)")
        for title, status, start, end, all_day in events:
            print(f"      → {title!r} {start:%a %d.%m %H:%M} – {end:%a %d.%m %H:%M}{' [весь день]' if all_day else ''}")
        if not ok or not events:
            for r in replies:
                print("      бот:", r.replace("\n", " ⏎ ")[:300])
    print(f"\n{len(results) - bad}/{len(results)} ok")

if __name__ == "__main__":
    asyncio.run(main())
