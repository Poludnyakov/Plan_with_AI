from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from account_service import (
    complete_web_login,
    consume_link_code,
    create_link_code,
    ensure_identity,
    intelligent_reminders_enabled,
    set_intelligent_reminders,
)
from reminder_service import (
    acknowledge_delivery,
    preference_text,
    reminder_preference,
    snooze_delivery,
    update_reminder_preference,
)
from unified_calendar import get_owned_entry

from .api import callback_button
from .handler_full import FullMaxUpdateHandler
from .models import MaxUser


class FeatureMaxUpdateHandler(FullMaxUpdateHandler):
    @staticmethod
    def reminder_panel(preference):
        return [
            [callback_button(
                f"🔔 Уведомления: {'вкл' if preference.enabled else 'выкл'}",
                "reminder_pref:enabled",
            )],
            [callback_button("Количество: изменить", "reminder_pref:frequency")],
            [callback_button(
                f"🌅 Сводка: {'вкл' if preference.daily_summary else 'выкл'}",
                "reminder_pref:summary",
            )],
            [callback_button(
                f"🕗 Время: {preference.summary_hour:02d}:00",
                "reminder_pref:summary_hour",
            )],
            [callback_button(
                f"⏰ Позже: {preference.snooze_minutes} мин",
                "reminder_pref:snooze",
            )],
            [callback_button(
                f"💳 AI: {'вкл' if preference.use_ai else 'выкл'}",
                "reminder_pref:ai",
            )],
        ]

    @staticmethod
    def profile_name(user: dict) -> str:
        first_name = str(user.get("first_name") or "").strip()
        last_name = str(user.get("last_name") or "").strip()
        full_name = " ".join(part for part in (first_name, last_name) if part)
        if full_name:
            return full_name
        username = str(user.get("username") or "").strip().lstrip("@")
        return username or "друг"

    async def welcome(
        self,
        user_id: int,
        name: str = "друг",
        returning: bool = False,
        chat_id: int | None = None,
    ) -> None:
        greeting = (
            f"👋 С возвращением, {name}!"
            if returning else
            f"👋 Привет, {name}! Добро пожаловать в «планиИруй!»."
        )
        destination = {"chat_id": chat_id} if chat_id is not None else {"user_id": user_id}
        await self.client.send_message(
            "✅ Бот запущен.\n\n" + greeting
            + "\n\nОтправьте текст, голосовое сообщение или откройте календарь кнопкой ниже.",
            **destination,
            buttons=[[self.app_button()]],
        )
        await self.client.send_message(
            "Календарь откроется внутри MAX и войдёт в ваш аккаунт автоматически.",
            **destination,
            buttons=[[self.app_button()]],
        )

    async def handle_update(self, update: dict, db: AsyncSession) -> None:
        if update.get("update_type") != "bot_started":
            await super().handle_update(update, db)
            return
        user_data = update.get("user") or {}
        user_id = int(user_data.get("user_id", user_data.get("id")))
        existing = (await db.execute(
            select(MaxUser).filter(MaxUser.max_user_id == user_id)
        )).scalar_one_or_none()
        await self.service.user(db, user_id)
        await ensure_identity(db, "max", user_id)
        payload = update.get("payload") or ""
        if payload.startswith("web_"):
            try:
                await complete_web_login(db, "max", user_id, payload[4:])
                await self.client.send_message(
                    "✅ Вход на сайт подтверждён. Можно вернуться в браузер.", user_id=user_id
                )
            except ValueError as error:
                await db.rollback()
                await self.client.send_message(f"❌ {error}", user_id=user_id)
            return
        await db.commit()
        await self.welcome(
            user_id,
            self.profile_name(user_data),
            returning=existing is not None,
            chat_id=int(update["chat_id"]) if update.get("chat_id") is not None else None,
        )

    async def handle_message(self, message: dict, db: AsyncSession) -> None:
        user_id = self.sender_id(message)
        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].casefold() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command == "/start":
            if argument.startswith("web_"):
                try:
                    await complete_web_login(db, "max", user_id, argument[4:])
                    await self.client.send_message(
                        "✅ Вход на сайт подтверждён. Можно вернуться в браузер.", user_id=user_id
                    )
                except ValueError as error:
                    await db.rollback()
                    await self.client.send_message(f"❌ {error}", user_id=user_id)
                return
            existing = (await db.execute(
                select(MaxUser).filter(MaxUser.max_user_id == user_id)
            )).scalar_one_or_none()
            await self.service.user(db, user_id)
            await ensure_identity(db, "max", user_id)
            await db.commit()
            sender = message.get("sender") or {}
            await self.welcome(
                user_id,
                self.profile_name(sender),
                returning=existing is not None,
            )
            return

        if command in {"/link", "/connect"}:
            if not argument:
                code = await create_link_code(db, "max", user_id)
                await self.client.send_message(
                    "🔗 **Объединение MAX и Telegram**\n\n"
                    f"Отправьте в Telegram-боте команду:\n`/link {code}`\n\n"
                    "Код действует 10 минут. После подключения календарь станет общим.",
                    user_id=user_id,
                )
                return
            try:
                identities = await consume_link_code(db, "max", user_id, argument)
                platforms = " и ".join("Telegram" if key == "telegram" else "MAX" for key in identities)
                await self.client.send_message(
                    f"✅ Аккаунты объединены: {platforms}. Календарь теперь общий.", user_id=user_id
                )
            except ValueError as error:
                await db.rollback()
                await self.client.send_message(f"❌ {error}", user_id=user_id)
            return

        if command in {"/smart_reminders", "/reminders"}:
            preference = await update_reminder_preference(
                db, "max", user_id, "platform"
            )
            await self.client.send_message(
                preference_text(preference),
                user_id=user_id,
                buttons=self.reminder_panel(preference),
            )
            return
        if command in {"/smart_reminders_on", "/smart_reminders_off"}:
            enabled = command.endswith("_on")
            preference = await reminder_preference(db, "max", user_id)
            if preference.use_ai != enabled:
                preference = await update_reminder_preference(
                    db, "max", user_id, "ai"
                )
            await self.client.send_message(
                "💳 AI-анализ времени включён и может расходовать средства Yandex Cloud."
                if enabled else
                "✅ Платный AI отключён. Используются бесплатные локальные правила.",
                user_id=user_id,
                buttons=self.reminder_panel(preference),
            )
            return
        await super().handle_message(message, db)

    async def handle_callback(self, update: dict, db: AsyncSession) -> None:
        callback = update.get("callback") or {}
        payload = callback.get("payload") or ""
        if not payload.startswith(("reminder_pref:", "reminder_ack:", "reminder_snooze:")):
            await super().handle_callback(update, db)
            return
        callback_id = callback.get("callback_id")
        user_id = int((callback.get("user") or {}).get("user_id"))
        if payload.startswith("reminder_pref:"):
            preference = await update_reminder_preference(
                db, "max", user_id, payload.split(":", 1)[1]
            )
            await self.client.answer_callback(
                callback_id,
                text=preference_text(preference),
                notification="Настройка сохранена",
                buttons=self.reminder_panel(preference),
            )
            return
        _, prefix, raw_event_id = payload.split(":", 2)
        entry = await get_owned_entry(db, "max", user_id, f"{prefix}:{raw_event_id}")
        if not entry:
            await self.client.answer_callback(callback_id, notification="Событие не найдено")
            return
        if payload.startswith("reminder_ack:"):
            await acknowledge_delivery(db, entry.source, entry.event.id, user_id)
            await self.client.answer_callback(
                callback_id, text=f"✅ Учтено: {entry.event.title}", notification="Учтено"
            )
            return
        try:
            snooze_at, minutes = await snooze_delivery(
                db, entry.source, entry.event.id, user_id, entry.timing.start_at
            )
        except ValueError as error:
            await self.client.answer_callback(callback_id, notification=str(error))
            return
        await self.client.answer_callback(
            callback_id,
            text=f"⏰ Напомню о «{entry.event.title}» в {snooze_at.astimezone():%H:%M}.",
            notification=f"Напомню через {minutes} мин",
        )
