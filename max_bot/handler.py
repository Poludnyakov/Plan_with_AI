import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .api import MaxApiClient, callback_button, open_app_button
from .calendar import delete_max_yandex, sync_max_yandex
from .config import MaxSettings
from .models import MaxEvent, MaxEventTiming, MaxUser
from unified_calendar import delete_linked_event, find_linked_conflict, get_owned_entry
from .service import CANCEL_RE, MaxEventService, format_interval


logger = logging.getLogger("MaxUpdateHandler")


class MaxUpdateHandler:
    def __init__(self, client: MaxApiClient, service: MaxEventService, settings: MaxSettings):
        self.client, self.service, self.settings = client, service, settings

    def app_button(self):
        return open_app_button("📅 Открыть календарь", self.settings.miniapp_name or None)

    async def welcome(self, user_id: int) -> None:
        await self.client.send_message(
            "Привет! Я **планиИруй! для MAX**. Напишите или пришлите голосом, например: "
            "«Контрольная по математике завтра с 13 до 14». Я покажу карточку перед добавлением.\n\n"
            "Удаление: «отмена контрольная завтра в 13». Изображение расписания тоже можно прислать.",
            user_id=user_id,
            buttons=[[self.app_button()]],
        )

    async def send_event_card(self, user_id: int, event: MaxEvent, source: str = "") -> None:
        timing = event.timing
        reminders = "\n".join(
            f"• {item.remind_at.astimezone():%d.%m в %H:%M}" for item in sorted(event.reminders, key=lambda x: x.remind_at)
        ) or "• Нет будущих напоминаний"
        text = (
            (source + "\n" if source else "") + f"**{event.title}**\n"
            f"🕒 {format_interval(timing.start_at, timing.end_at)}\n"
            + (f"📍 {event.description}\n" if event.description else "")
            + f"\n**Напоминания:**\n{reminders}\n\nДобавить мероприятие в календарь?"
        )
        await self.client.send_message(text, user_id=user_id, buttons=[[
            callback_button("✅ Подтвердить", f"confirm:{event.id}"),
            callback_button("❌ Отменить", f"cancel:{event.id}"),
        ]])

    async def handle_update(self, update: dict, db: AsyncSession) -> None:
        kind = update.get("update_type")
        if kind == "bot_started":
            user_id = int(update["user"]["user_id"])
            await self.service.user(db, user_id)
            await db.commit()
            await self.welcome(user_id)
        elif kind == "message_created":
            await self.handle_message(update.get("message") or {}, db)
        elif kind == "message_callback":
            await self.handle_callback(update, db)

    @staticmethod
    def sender_id(message: dict) -> int:
        return int((message.get("sender") or {}).get("user_id"))

    async def handle_message(self, message: dict, db: AsyncSession) -> None:
        user_id = self.sender_id(message)
        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        if text.startswith("/start"):
            await self.welcome(user_id)
            return
        if text.startswith(("/calendar", "/list")):
            rows = await self.service.list_confirmed(db, user_id)
            if not rows:
                answer = "Календарь пока пуст. Напишите первое мероприятие."
            else:
                answer = "**Ближайшие мероприятия:**\n" + "\n".join(
                    f"• {event.title} — {format_interval(timing.start_at, timing.end_at, zone)}"
                    for event, timing, zone in rows
                )
            await self.client.send_message(answer, user_id=user_id, buttons=[[self.app_button()]])
            return
        if text and CANCEL_RE.match(text):
            await self.handle_cancellation(user_id, text, db)
            return
        attachments = body.get("attachments") or []
        try:
            if text and not text.startswith("/"):
                events = await self.service.from_text(db, user_id, text)
                source = ""
            else:
                attachment = next((x for x in attachments if x.get("type") in {"image", "audio"}), None)
                if not attachment:
                    await self.client.send_message("Пришлите текст, голосовое сообщение или изображение расписания.", user_id=user_id)
                    return
                content = await self.client.download((attachment.get("payload") or {})["url"])
                if attachment["type"] == "image":
                    events = await self.service.from_image(db, user_id, content)
                    source = "🖼 Распознано из изображения"
                else:
                    events = await self.service.from_voice(db, user_id, content)
                    source = "🎙 Распознано из голосового сообщения"
            if not events:
                await self.client.send_message("Не удалось найти мероприятие. Укажите дату и время точнее.", user_id=user_id)
            for event in events:
                await self.send_event_card(user_id, event, source)
        except Exception as error:
            await db.rollback()
            logger.error("MAX input processing failed: %s", error, exc_info=True)
            await self.client.send_message(f"❌ Не удалось обработать сообщение: {error}", user_id=user_id)

    async def handle_cancellation(self, user_id: int, text: str, db: AsyncSession) -> None:
        if not CANCEL_RE.match(text).group(1).strip():
            await self.client.send_message("Напишите, что удалить. Например: «отмена контрольная по русскому».", user_id=user_id)
            return
        candidates = await self.service.cancellation_candidates(db, user_id, text)
        if not candidates:
            await self.client.send_message("Не нашёл подходящее мероприятие. Уточните название, дату или время.", user_id=user_id)
            return
        if len(candidates) == 1:
            _, event, timing, zone = candidates[0]
            description = f"{event.title} — {format_interval(timing.start_at, timing.end_at, zone)}"
            await delete_linked_event(
                db, "max", user_id, getattr(event, "_calendar_ref", f"m:{event.id}")
            )
            await self.client.send_message(f"🗑 Удалено: {description}", user_id=user_id)
            return
        lines, buttons = ["Нашёл несколько мероприятий. Выберите, какое удалить:"], []
        for index, (_, event, timing, zone) in enumerate(candidates[:8], 1):
            lines.append(f"{index}. {event.title} — {format_interval(timing.start_at, timing.end_at, zone)}")
            ref = getattr(event, "_calendar_ref", f"m:{event.id}")
            buttons.append([callback_button(f"🗑 {event.title[:35]}", f"delete:{ref}")])
        await self.client.send_message("\n".join(lines), user_id=user_id, buttons=buttons)

    async def owned(self, db: AsyncSession, user_id: int, event_id: int):
        result = await db.execute(
            select(MaxEvent, MaxEventTiming)
            .join(MaxUser).join(MaxEventTiming)
            .filter(MaxEvent.id == event_id, MaxUser.max_user_id == user_id)
        )
        return result.first()

    async def delete_owned(self, db: AsyncSession, user_id: int, event_id: int):
        row = await self.owned(db, user_id, event_id)
        if not row:
            return None
        event, timing = row
        confirmed = event.status == "confirmed"
        await db.delete(event)
        await db.commit()
        if confirmed:
            asyncio.create_task(delete_max_yandex(event.id))
        return event, timing

    async def handle_callback(self, update: dict, db: AsyncSession) -> None:
        callback = update.get("callback") or {}
        callback_id, payload = callback.get("callback_id"), callback.get("payload") or ""
        user_id = int((callback.get("user") or {}).get("user_id"))
        try:
            action, event_text = payload.split(":", 1)
        except (ValueError, TypeError):
            await self.client.answer_callback(callback_id, notification="Неизвестная команда")
            return
        try:
            if action == "delete" and ":" in event_text:
                entry = await get_owned_entry(db, "max", user_id, event_text)
                if not entry:
                    await self.client.answer_callback(callback_id, notification="Событие уже удалено")
                    return
                await delete_linked_event(db, "max", user_id, event_text)
                await self.client.answer_callback(
                    callback_id,
                    text=f"🗑 {entry.event.title}\n{format_interval(entry.timing.start_at, entry.timing.end_at)}\nУдалено.",
                    notification="Удалено",
                )
                return
            event_id = int(event_text)
            if action in {"cancel", "delete"}:
                row = await self.delete_owned(db, user_id, event_id)
                if not row:
                    await self.client.answer_callback(callback_id, notification="Событие уже удалено")
                    return
                event, timing = row
                await self.client.answer_callback(callback_id, text=f"🗑 {event.title}\n{format_interval(timing.start_at, timing.end_at)}\nУдалено.", notification="Удалено")
                return
            if action == "complete":
                row = await self.owned(db, user_id, event_id)
                if not row:
                    await self.client.answer_callback(callback_id, notification="Событие не найдено")
                    return
                row[0].is_completed = not row[0].is_completed
                await db.commit()
                await self.client.answer_callback(callback_id, notification="Статус обновлён")
                return
            if action != "confirm":
                await self.client.answer_callback(callback_id, notification="Неизвестная команда")
                return
            row = await self.owned(db, user_id, event_id)
            if not row:
                await self.client.answer_callback(callback_id, notification="Событие не найдено")
                return
            event, timing = row
            conflict_result = await db.execute(
                select(MaxEvent, MaxEventTiming).join(MaxEventTiming).filter(
                    MaxEvent.user_id == event.user_id, MaxEvent.id != event.id,
                    MaxEvent.status == "confirmed", MaxEventTiming.start_at < timing.end_at,
                    MaxEventTiming.end_at > timing.start_at,
                )
            )
            local_conflict = conflict_result.first()
            linked_conflict = None
            if not local_conflict:
                try:
                    linked_conflict = await find_linked_conflict(
                        db, "max", user_id, timing.start_at, timing.end_at,
                        exclude_ref=f"m:{event.id}",
                    )
                except StopAsyncIteration:
                    linked_conflict = None
            if local_conflict or linked_conflict:
                other = local_conflict[0] if local_conflict else linked_conflict.event
                other_time = local_conflict[1] if local_conflict else linked_conflict.timing
                await db.delete(event)
                await db.commit()
                await self.client.answer_callback(
                    callback_id,
                    text=f"⚠️ Мероприятия перекрываются.\nУже запланировано: «{other.title}» — {format_interval(other_time.start_at, other_time.end_at)}.\n\nНовое мероприятие не добавлено.",
                    notification="Мероприятия перекрываются",
                )
                return
            if event.status != "confirmed":
                event.status = "confirmed"
                await db.commit()
                asyncio.create_task(sync_max_yandex(event.title, timing.start_at, timing.end_at, event.description or "", event.id))
            await self.client.answer_callback(callback_id, text=f"✅ {event.title}\n{format_interval(timing.start_at, timing.end_at)}\nДобавлено в календарь.", notification="Подтверждено")
        except Exception as error:
            await db.rollback()
            logger.error("MAX callback failed: %s", error, exc_info=True)
            await self.client.answer_callback(callback_id, notification="Не удалось выполнить действие")
