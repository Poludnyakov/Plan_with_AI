from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .api import callback_button
from .handler import MaxUpdateHandler
from .models import MaxReminder, MaxUser
from .service import aware, format_interval
from .state_models import MaxPendingAction


class FullMaxUpdateHandler(MaxUpdateHandler):
    """Adds the multi-message reminder flow missing from the stateless API adapter."""

    async def send_event_card(self, user_id, event, source="") -> None:
        timing = event.timing
        zone = ZoneInfo("Europe/Moscow")
        reminders = "\n".join(
            f"• {item.remind_at.astimezone(zone):%d.%m в %H:%M}"
            for item in sorted(event.reminders, key=lambda value: value.remind_at)
        ) or "• Нет будущих напоминаний"
        text = (
            (source + "\n" if source else "") + f"**{event.title}**\n"
            f"🕒 {format_interval(timing.start_at, timing.end_at, zone.key)}\n"
            + (f"📍 {event.description}\n" if event.description else "")
            + f"\n**Напоминания:**\n{reminders}\n\nДобавить мероприятие в календарь?"
        )
        await self.client.send_message(text, user_id=user_id, buttons=[
            [callback_button("✅ Подтвердить", f"confirm:{event.id}"), callback_button("❌ Отменить", f"cancel:{event.id}")],
            [callback_button("🔔 Добавить напоминание", f"reminder:{event.id}")],
        ])

    async def handle_message(self, message: dict, db: AsyncSession) -> None:
        user_id = self.sender_id(message)
        pending = await db.get(MaxPendingAction, user_id)
        if pending:
            text = ((message.get("body") or {}).get("text") or "").strip()
            if text.casefold() in {"/cancel", "отмена"}:
                await db.delete(pending)
                await db.commit()
                await self.client.send_message("❌ Добавление напоминания отменено.", user_id=user_id)
                return
            try:
                parsed = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=ZoneInfo("Europe/Moscow"))
            except ValueError:
                await self.client.send_message(
                    "⚠️ Введите дату строго в формате `ДД.ММ.ГГГГ ЧЧ:ММ`, например `29.08.2026 14:15`, или отправьте /cancel.",
                    user_id=user_id,
                )
                return
            row = await self.owned(db, user_id, pending.event_id)
            if not row:
                await db.delete(pending)
                await db.commit()
                await self.client.send_message("Событие уже удалено.", user_id=user_id)
                return
            event, timing = row
            parsed_utc = parsed.astimezone(timezone.utc)
            if parsed_utc <= datetime.now(timezone.utc) or parsed_utc >= aware(timing.start_at):
                await self.client.send_message("⚠️ Напоминание должно быть в будущем и раньше начала мероприятия.", user_id=user_id)
                return
            db.add(MaxReminder(event_id=event.id, remind_at=parsed_utc, status="pending"))
            await db.delete(pending)
            await db.commit()
            refreshed = await self.service.event_for_user(db, user_id, event.id)
            await self.client.send_message("✅ Напоминание добавлено.", user_id=user_id)
            await self.send_event_card(user_id, refreshed)
            return
        await super().handle_message(message, db)

    async def handle_callback(self, update: dict, db: AsyncSession) -> None:
        callback = update.get("callback") or {}
        payload = callback.get("payload") or ""
        if not payload.startswith("reminder:") and payload != "cancel_reminder":
            await super().handle_callback(update, db)
            return
        callback_id = callback.get("callback_id")
        user_id = int((callback.get("user") or {}).get("user_id"))
        if payload == "cancel_reminder":
            pending = await db.get(MaxPendingAction, user_id)
            if pending:
                await db.delete(pending)
                await db.commit()
            await self.client.answer_callback(callback_id, notification="Добавление отменено")
            return
        try:
            event_id = int(payload.split(":", 1)[1])
        except ValueError:
            await self.client.answer_callback(callback_id, notification="Некорректное событие")
            return
        if not await self.owned(db, user_id, event_id):
            await self.client.answer_callback(callback_id, notification="Событие не найдено")
            return
        existing = await db.get(MaxPendingAction, user_id)
        if existing:
            existing.event_id, existing.action = event_id, "add_reminder"
        else:
            db.add(MaxPendingAction(max_user_id=user_id, action="add_reminder", event_id=event_id))
        await db.commit()
        await self.client.answer_callback(callback_id, notification="Жду дату и время")
        await self.client.send_message(
            "➕ **Добавление напоминания**\nОтправьте дату и время: `ДД.ММ.ГГГГ ЧЧ:ММ`.\nНапример: `29.08.2026 14:15`.",
            user_id=user_id,
            buttons=[[callback_button("❌ Отменить добавление", "cancel_reminder")]],
        )
