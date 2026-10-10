import asyncio, sys, time
from harness import TgSim, user_events
from database import init_db
TEXTS_ALL = [
    "встреча в 10",
    "сегодня в 9 вечера сериал",
    "до пятницы сдать эссе",
    "каждый вторник в 18 бассейн",
    "напомни купить молоко",
    "завтра в 25:00 встреча",
    "30 февраля встреча с другом",
    "вчера в 10 была встреча",
    "завтра с 18 до 16 репетиция",
    "встреча с Иваном Петровичем Сидоровым завтра в 11 в кабинете 305",
    "позвонить по номеру +7 999 123-45-67 завтра в 10",
    "на следующей неделе в среду в 15 вебинар",
    "через 2 часа созвон",
    "Завтра:\n9:00 — зарядка\n10:30 — пара по химии до 12:00\n18:00 — встреча клуба",
    "в обед завтра встреча с другом",
    "запиши меня к парикмахеру на субботу на 11",
    "мне нужно завтра к 9 быть в универе",
    "собрание 20.10.26 в 17-00",
]
RUN = int(time.time())
TEXTS = [TEXTS_ALL[i] for i in map(int, sys.argv[1:])] if sys.argv[1:] else TEXTS_ALL
async def one(i, t, sem):
    async with sem:
        sim = TgSim(user_id=810000000 + (RUN % 10000) * 100 + i)
        out = await sim.text(t)
        ev = await user_events(sim.user.id)
        rep = [x for n, _, x, _ in out if n == "SendMessage" and x and not x.startswith("⏳")]
        lines = [f"#{i} {t!r}"]
        lines += [f"     → {e[0]!r} {e[2]:%a %d.%m %H:%M} – {e[3]:%a %d.%m %H:%M}{' [весь день]' if e[4] else ''}" for e in ev]
        if not ev:
            lines += ["     бот: " + r.replace(chr(10), ' ⏎ ')[:250] for r in rep]
        else:
            lines += ["     карточка: " + rep[-1].replace(chr(10), ' ⏎ ')[:250]] if rep else []
        print("\n".join(lines), flush=True)
async def main():
    await init_db()
    sem = asyncio.Semaphore(6)
    await asyncio.gather(*(one(i, t, sem) for i, t in enumerate(TEXTS)))
asyncio.run(main())
