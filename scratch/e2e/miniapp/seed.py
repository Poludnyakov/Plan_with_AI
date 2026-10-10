"""Seed one Telegram user for the browser test and print signed initData."""
import asyncio, hashlib, hmac, json, sys, time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import harness  # DB = planiruy_e2e
from sqlalchemy import select
import database
from database import init_db
from models import User
from account_service import ensure_identity
from unified_calendar import create_linked_event
from schedule_service import create_import_draft, set_draft_range, confirm_import

TOKEN = "123456:TESTTOKENtesttokenTESTTOKENtest1234"  # the local test server's fake token
TG = int(sys.argv[1])
PLATFORM = sys.argv[2] if len(sys.argv) > 2 else "telegram"
if PLATFORM == "max":
    TOKEN = "maxtesttoken-local-only"
Z = ZoneInfo("Europe/Moscow")

async def main():
    await init_db()
    async with database.async_session_maker() as db:
        if PLATFORM == "max":
            from max_bot.models import MaxUser
            if not (await db.execute(select(MaxUser).filter(MaxUser.max_user_id == TG))).scalar_one_or_none():
                db.add(MaxUser(max_user_id=TG)); await db.commit()
        elif not (await db.execute(select(User).filter(User.tg_id == TG))).scalar_one_or_none():
            db.add(User(tg_id=TG, timezone="Europe/Moscow")); await db.commit()
        await ensure_identity(db, PLATFORM, TG); await db.commit()
        today = datetime.now(Z).date()
        tomorrow = today + timedelta(days=1)
        at = lambda d, h, m=0: datetime(d.year, d.month, d.day, h, m, tzinfo=Z)
        await create_linked_event(db, PLATFORM, TG, "Утренняя встреча", "кабинет 5", at(tomorrow, 10), at(tomorrow, 11))
        await create_linked_event(db, PLATFORM, TG, "<img src=x onerror=alert(1)> Тест XSS", "<b>описание</b>", at(tomorrow, 14), at(tomorrow, 15))
        await create_linked_event(db, PLATFORM, TG, "Командировка", "", at(tomorrow + timedelta(days=2), 0), at(tomorrow + timedelta(days=4), 0), None, True)
        await create_linked_event(db, PLATFORM, TG, "Ночная смена", "", at(tomorrow, 23), at(tomorrow + timedelta(days=1), 1))
        draft = await create_import_draft(db, PLATFORM, TG, {"confidence": 1, "slots": [
            {"title": "Матанализ", "weekday": tomorrow.weekday(), "start_time": "12:40", "end_time": "14:10", "week_pattern": "every", "description": "ауд. 101"}]})
        await set_draft_range(db, draft, today - timedelta(days=7), today + timedelta(days=60))
        await confirm_import(db, PLATFORM, TG, draft.id)
    user = json.dumps({"id": TG, "user_id": TG, "first_name": "Тест", "language_code": "ru"}, ensure_ascii=False, separators=(",", ":"))
    fields = {"auth_date": str(int(time.time())), "query_id": "AAE", "user": user}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    print("INITDATA=" + urlencode(fields))
asyncio.run(main())
