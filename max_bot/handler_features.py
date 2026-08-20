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

from .handler_full import FullMaxUpdateHandler
from .models import MaxUser


class FeatureMaxUpdateHandler(FullMaxUpdateHandler):
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
            enabled = await intelligent_reminders_enabled(db, "max", user_id)
            await db.commit()
            state = "включены" if enabled else "выключены"
            await self.client.send_message(
                f"🧠 Интеллектуальные напоминания **{state}**.\n\n"
                "Включить: /smart_reminders_on\nВыключить: /smart_reminders_off",
                user_id=user_id,
            )
            return
        if command in {"/smart_reminders_on", "/smart_reminders_off"}:
            enabled = command.endswith("_on")
            await set_intelligent_reminders(db, "max", user_id, enabled)
            await self.client.send_message(
                "🧠 Интеллектуальные напоминания включены."
                if enabled else
                "🔕 Интеллектуальные напоминания выключены. Останутся стандартные интервалы.",
                user_id=user_id,
            )
            return
        await super().handle_message(message, db)
