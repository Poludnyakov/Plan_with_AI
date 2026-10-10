"""The same dialogues through the MAX handler with a recording client."""
import asyncio, itertools, json, sys, time
from pathlib import Path
import harness  # env, DB
import database
from database import init_db
from max_bot.config import MaxSettings
from max_bot.handler_features import FeatureMaxUpdateHandler
from max_bot.service import MaxEventService

HERE = Path(__file__).resolve().parent
ids = itertools.count(1)
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"   {'✔' if cond else '✘'} {name}{(' — ' + detail[:300]) if detail and not cond else ''}")


class FakeMaxClient:
    def __init__(self):
        self.sent = []
        self.files = {}
        self.bot_info = {}  # the real client reads it from /me

    async def send_message(self, text, *, user_id=None, chat_id=None, buttons=None, format="markdown"):
        self.sent.append(("message", text, buttons))
        return {"message": {"body": {"mid": f"m{next(ids)}"}}}

    async def answer_callback(self, callback_id, *, text=None, notification=None, buttons=None, format="markdown"):
        self.sent.append(("callback", text or notification, buttons))
        return {}

    async def download(self, url):
        return self.files[url]

    async def set_commands(self, commands):
        return {}

    async def __getattr__(self, name):
        raise AttributeError(name)


class MaxSim:
    def __init__(self, handler, client, user_id):
        self.h, self.c, self.user_id = handler, client, user_id

    async def _run(self, update):
        start = len(self.c.sent)
        async with database.async_session_maker() as db:
            await self.h.handle_update(update, db)
        out = self.c.sent[start:]
        for kind, text, buttons in out:
            labels = [b.get("text") + "→" + str(b.get("payload", "")) for row in (buttons or []) for b in row]
            print(f"  {kind}: {(text or '').strip()}" + (f" | кнопки: {labels}" if labels else ""))
        return out

    async def text(self, text, attachments=None):
        print(f"\n» {text}")
        return await self._run({"update_type": "message_created", "message": {
            "sender": {"user_id": self.user_id}, "recipient": {"user_id": 1},
            "body": {"mid": f"u{next(ids)}", "text": text, "attachments": attachments or []}}})

    async def file(self, path, caption=""):
        url = f"https://files.example/{next(ids)}"
        self.c.files[url] = Path(path).read_bytes()
        print(f"\n» файл {Path(path).name} {caption!r}")
        return await self._run({"update_type": "message_created", "message": {
            "sender": {"user_id": self.user_id}, "recipient": {"user_id": 1},
            "body": {"mid": f"u{next(ids)}", "text": caption, "attachments": [
                {"type": "file", "payload": {"url": url, "filename": Path(path).name}}]}}})

    async def press(self, label):
        for kind, text, buttons in reversed(self.c.sent):
            for row in buttons or []:
                for b in row:
                    if label.lower() in (b.get("text") or "").lower() and b.get("payload"):
                        print(f"  [нажата «{b['text']}»]")
                        return await self._run({"update_type": "message_callback", "callback": {
                            "callback_id": f"cb{next(ids)}", "payload": b["payload"],
                            "user": {"user_id": self.user_id}}})
        raise LookupError(label)


def joined(out):
    return "\n".join(t or "" for _, t, _ in out)


async def main():
    await init_db()
    client = FakeMaxClient()
    handler = FeatureMaxUpdateHandler(client, MaxEventService(), MaxSettings())
    base = 700000000 + int(time.time()) % 100000 * 10

    if "--autumn" in sys.argv:
        sim = MaxSim(handler, client, base + 4)
        await sim.file(harness.ROOT / "ОСЕНЬ 26_27_тест.xlsx", "AI360")
        await sim.text("522")
        out = await sim.text("с 1 сентября по 31 декабря")
        print("ПОЛНЫЙ ОТВЕТ:", joined(out)[:700])
        return
    sim = MaxSim(handler, client, base + 1)
    await sim.text("завтра с 15 до 17 встреча с инвестором")
    out = await sim.press("Подтвердить")
    check("MAX: событие подтверждено", "календар" in joined(out).lower() or "добавлено" in joined(out).lower(), joined(out))
    await sim.text("завтра в 16:00 созвон с бухгалтером")
    out = await sim.press("Подтвердить")
    check("MAX: пересечение обнаружено", "перекрыва" in joined(out).lower() or "пересека" in joined(out).lower(), joined(out))
    out = await sim.text("в среду в пол третьего стоматолог")
    check("MAX: «пол третьего» = 14:30", "14:30" in joined(out), joined(out))
    out = await sim.text("вчера в 10 была встреча")
    check("MAX: предупреждение о прошедшем времени", "уже прошло" in joined(out), joined(out))
    out = await sim.text("Олимпиада по программированию 25 октября в 10")
    check("MAX: название не обрезано обезличиванием", "Олимпиада по программированию" in joined(out), joined(out))

    out = await sim.text("завтра весь день командировка")
    check("MAX: событие на весь день показано как «весь день»", "весь день" in joined(out), joined(out))
    out = await sim.press("Подтвердить")
    out = await sim.text("завтра в 20 ужин")
    check("MAX: карточка предупреждает о событии на весь день", "на весь день или дольше" in joined(out), joined(out))
    out = await sim.text("перенеси его на 21:00")
    check("MAX: «перенеси его» понимает последнее событие", "21:00" in joined(out) or "ожидает подтверждения" in joined(out), joined(out))

    sim = MaxSim(handler, client, base + 2)
    out = await sim.file(HERE / "xlsx/3_two_groups.xlsx")
    check("MAX: вопрос о группе", "групп" in joined(out).lower(), joined(out))
    out = await sim.text("ИВТ-22")
    check("MAX: импорт только ИВТ-22 (4 пары)", "4 из 4" in joined(out) or "Создано элементов: 4" in joined(out), joined(out))

    sim = MaxSim(handler, client, base + 3)
    out = await sim.file(HERE / "xlsx/1_list.xlsx")
    out = await sim.text("12 октября в 9:30 встреча с деканом")
    check("MAX: карточка показывает пару из расписания", "по расписанию" in joined(out), joined(out))

    sim = MaxSim(handler, client, base + 4)
    await sim.file(harness.ROOT / "ОСЕНЬ 26_27_тест.xlsx", "AI360")
    await sim.text("522")
    out = await sim.text("с 1 сентября по 31 декабря")
    print("ПОЛНЫЙ ОТВЕТ:", joined(out)[:600])
    check("MAX: ОСЕНЬ AI360→522→период: 23 занятия в 2026", "23 из 23" in joined(out) and "2026" in joined(out), joined(out))

    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} проверок MAX прошло")

if __name__ == "__main__":
    asyncio.run(main())
