import logging

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
from onboarding import commands_text, first_run_buttons, tour_page
from reminder_summary import daily_summary_text, weekly_summary_text
from reminder_service import (
    acknowledge_delivery,
    preference_text,
    reminder_preference,
    snooze_delivery,
    update_reminder_preference,
)
from calendar_feed_service import connect_text, feed_links, feed_token
from calendar_import_service import (
    CALDAV_HELP,
    IMPORT_HELP,
    MAX_ICS_BYTES,
    CalendarImportError,
    expects_caldav,
    find_ics_link,
    import_from_caldav,
    import_from_file,
    import_from_link,
    is_ics_filename,
    parse_caldav_credentials,
    result_text,
    stop_waiting_caldav,
    undo_imports,
    undo_payload,
    wait_for_caldav,
)
from history_service import (
    PAGE_SIZE,
    addition_history,
    delete_history_item,
    deletion_question,
    find_history_item,
    history_page,
)
from unified_calendar import ensure_platform_user, get_owned_entry
from usage_guide import usage_guide
from schedule_service import discard_pending_imports

from .api import callback_button, link_button
from .handler_full import FullMaxUpdateHandler
from .models import MaxUser


logger = logging.getLogger("MaxCalendarImport")


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
            + "\n\nОтправьте текст, голосовое сообщение или откройте календарь кнопкой ниже."
            + ("" if returning else "\n\n" + commands_text("max")),
            **destination,
            format=None,
            buttons=[
                [self.app_button()],
                [callback_button("ℹ️ Как пользоваться", "usage_help")],
                [callback_button("📖 Обучение за минуту", "tour:start")],
            ],
        )
        if not returning:
            # The first start: the tour begins right away, the calendar is one tap away.
            text, _ = tour_page("max", "tour:0")
            await self.client.send_message(text, **destination, format=None, buttons=self.draw(first_run_buttons()))
            return
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
        cleared_sources, cleared_drafts = await discard_pending_imports(db, "max", user_id)
        await db.commit()
        if cleared_sources or cleared_drafts:
            await self.client.send_message(
                "Предыдущий незавершённый импорт расписания отменён.",
                user_id=user_id,
            )
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
            cleared_sources, cleared_drafts = await discard_pending_imports(db, "max", user_id)
            await db.commit()
            if cleared_sources or cleared_drafts:
                await self.client.send_message(
                    "Предыдущий незавершённый импорт расписания отменён.",
                    user_id=user_id,
                )
            sender = message.get("sender") or {}
            await self.welcome(
                user_id,
                self.profile_name(sender),
                returning=existing is not None,
            )
            return

        if command in {"/help", "/instruction", "/guide"}:
            await self.client.send_message(usage_guide("max"), user_id=user_id)
            return

        if command == "/history":
            text, buttons = await self.history_view(db, user_id)
            await self.client.send_message(text, user_id=user_id, format=None, buttons=buttons)
            return

        if command in {"/calendars", "/subscribe", "/ics"}:
            await self.send_calendar_feed(
                user_id, db, reset=argument.strip().casefold() in {"new", "reset", "сброс", "новая"}
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

        if command in {"/tour", "/onboarding"}:
            text, rows = tour_page("max", "tour:0")
            await self.client.send_message(text, user_id=user_id, format=None, buttons=self.draw(rows))
            return
        if command in {"/today", "/week"}:
            await self.send_summary(db, user_id, command[1:])
            return
        if command == "/import":
            await self.client.send_message(IMPORT_HELP, user_id=user_id, format=None)
            return
        if command == "/caldav":
            wait_for_caldav("max", user_id)
            await self.client.send_message(CALDAV_HELP, user_id=user_id, format=None)
            return
        if await self.handle_calendar_import(message, user_id, text, db):
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

    def draw(self, rows) -> list[list[dict]]:
        """Neutral buttons (see onboarding.py) as MAX buttons."""
        drawn = []
        for row in rows:
            line = []
            for kind, text, value in row:
                if kind == "copy":
                    line.append({"type": "clipboard", "text": text, "payload": value})
                elif kind == "app":
                    line.append(self.app_button())
                else:
                    line.append(callback_button(text, value))
            drawn.append(line)
        return drawn

    def summary_buttons(self, kind: str) -> list[list[dict]]:
        other = ("callback", "📊 План на неделю", "summary_switch:week") if kind == "today" else ("callback", "🌅 План на сегодня", "summary_switch:today")
        return self.draw([[other], [("app", "📅 Открыть календарь", "")]])

    async def summary_text(self, db: AsyncSession, user_id: int, kind: str) -> str:
        user = await self.service.user(db, user_id)
        build = weekly_summary_text if kind == "week" else daily_summary_text
        return await build(db, "max", user_id, user.timezone)

    async def send_summary(self, db: AsyncSession, user_id: int, kind: str) -> None:
        await self.client.send_message(
            await self.summary_text(db, user_id, kind), user_id=user_id, buttons=self.summary_buttons(kind)
        )

    async def handle_calendar_import(self, message: dict, user_id: int, text: str, db: AsyncSession) -> bool:
        """An .ics file, an iCal link or the CalDAV access asked by /caldav."""
        attachments = (message.get("body") or {}).get("attachments") or []
        document = next((item for item in attachments if item.get("type") == "file"), None)
        payload = (document or {}).get("payload") or {}
        if document and is_ics_filename(
            self.attachment_name(document), payload.get("mime_type") or payload.get("content_type")
        ):
            size = payload.get("size") or document.get("size")
            if size is not None and int(size) > MAX_ICS_BYTES:
                await self.client.send_message("❌ Календарь слишком большой: максимум 5 МБ.", user_id=user_id)
                return True
            url = self.attachment_url(document)
            if not url:
                await self.client.send_message("❌ MAX не передал ссылку для скачивания файла.", user_id=user_id)
                return True
            await self.client.send_message("⏳ Загружаю события из календаря…", user_id=user_id)

            async def from_file():
                return await import_from_file(
                    db, "max", user_id, await self.client.download(url), self.attachment_name(document)
                )
            await self.finish_calendar_import(user_id, db, from_file)
            return True
        if not text or text.startswith("/"):
            if text.startswith("/"):
                stop_waiting_caldav("max", user_id)
            return False
        link = find_ics_link(text)
        if link:
            await self.client.send_message("⏳ Загружаю календарь по ссылке…", user_id=user_id)
            await self.finish_calendar_import(user_id, db, lambda: import_from_link(db, "max", user_id, link))
            return True
        if not expects_caldav("max", user_id):
            return False
        stop_waiting_caldav("max", user_id)
        credentials = parse_caldav_credentials(text)
        if not credentials:
            await self.client.send_message(
                "Не понял данные для CalDAV: нужны три строки — сервер, логин и пароль приложения. "
                "Отправьте /caldav, чтобы попробовать снова.",
                user_id=user_id, format=None,
            )
            return True
        note = ""
        message_id = (message.get("body") or {}).get("mid")
        try:
            if not message_id:
                raise ValueError("no message id")
            await self.client.delete_message(message_id)  # the password should not stay in the chat
        except Exception:
            note = "\nУдалите, пожалуйста, сообщение с паролем из чата."
        await self.client.send_message("⏳ Подключаюсь к календарю по CalDAV…" + note, user_id=user_id, format=None)
        await self.finish_calendar_import(
            user_id, db, lambda: import_from_caldav(db, "max", user_id, *credentials)
        )
        return True

    async def finish_calendar_import(self, user_id: int, db: AsyncSession, run) -> None:
        try:
            results = await run()
            user = await ensure_platform_user(db, "max", user_id)
            payload = undo_payload(results)
            buttons = [[callback_button("↩️ Отменить импорт", payload)]] if payload else []
            await self.client.send_message(
                result_text(results, user.timezone), user_id=user_id, format=None,
                buttons=buttons + [[self.app_button()]],
            )
        except CalendarImportError as error:
            await db.rollback()
            await self.client.send_message(f"❌ {error}", user_id=user_id, format=None)
        except Exception as error:
            await db.rollback()
            logger.error("MAX calendar import failed: %s", error, exc_info=True)
            await self.client.send_message(
                "❌ Не удалось загрузить календарь. Попробуйте ещё раз позже.", user_id=user_id
            )

    async def history_view(self, db: AsyncSession, user_id: int, offset: int = 0):
        text, page, more = history_page(await addition_history(db, "max", user_id), offset)
        numbered = [
            callback_button(f"🗑 {number}", f"hist:ask:{item.ref}")
            for number, item in enumerate(page, offset + 1) if item.deletable
        ]
        buttons = [numbered[index:index + 4] for index in range(0, len(numbered), 4)]
        navigation = []
        if offset:
            navigation.append(callback_button("⬅️ Новее", f"hist:page:{max(0, offset - PAGE_SIZE)}"))
        if more:
            navigation.append(callback_button("➡️ Ещё", f"hist:page:{offset + PAGE_SIZE}"))
        if navigation:
            buttons.append(navigation)
        return text, buttons

    async def handle_history_callback(self, callback: dict, payload: str, db: AsyncSession) -> None:
        callback_id = callback.get("callback_id")
        user_id = int((callback.get("user") or {}).get("user_id"))
        _, action, ref = (payload.split(":", 2) + [""])[:3]
        if action in {"page", "no"}:
            text, buttons = await self.history_view(db, user_id, int(ref or 0) if action == "page" else 0)
            await self.client.answer_callback(
                callback_id, text=text, buttons=buttons, format=None,
                notification="Ничего не удалено" if action == "no" else None,
            )
            return
        if action == "ask":
            item = await find_history_item(db, "max", user_id, ref)
            if not item or not item.deletable:
                await self.client.answer_callback(callback_id, notification="Запись уже удалена")
                return
            await self.client.answer_callback(
                callback_id, text=deletion_question(item), format=None,
                buttons=[[callback_button("🗑 Да, удалить", f"hist:del:{ref}"), callback_button("Отмена", "hist:no:")]],
            )
            return
        if action == "del":
            try:
                result = await delete_history_item(db, "max", user_id, ref)
            except Exception:
                await db.rollback()
                await self.client.answer_callback(callback_id, notification="Не удалось удалить")
                return
            await self.client.answer_callback(
                callback_id, text=result, notification="Удалено", format=None,
                buttons=[[callback_button("📜 К истории", "hist:page:0")]],
            )
            return
        await self.client.answer_callback(callback_id, notification="Неизвестное действие")

    async def send_calendar_feed(self, user_id: int, db: AsyncSession, reset: bool = False) -> None:
        token = await feed_token(db, "max", user_id, reset=reset)
        links = feed_links(token, self.settings.public_base_url)
        prefix = "♻️ Старая ссылка отключена.\n\n" if reset else ""
        await self.client.send_message(
            prefix + connect_text(links), user_id=user_id, format=None,
            buttons=[
                [link_button("📆 Подключить календарь", links["connect"])],
                [link_button("📅 Google Календарь", links["google"])],
                [callback_button("♻️ Новая ссылка", "calendar_feed:reset")],
            ],
        )

    async def handle_callback(self, update: dict, db: AsyncSession) -> None:
        callback = update.get("callback") or {}
        payload = callback.get("payload") or ""
        if payload.startswith("hist:"):
            await self.handle_history_callback(callback, payload, db)
            return
        if payload == "tour:start":
            # From the welcome: keep its buttons and start the tour below.
            user_id = int((callback.get("user") or {}).get("user_id"))
            text, rows = tour_page("max", payload)
            await self.client.answer_callback(callback.get("callback_id"), notification="Обучение")
            await self.client.send_message(text, user_id=user_id, format=None, buttons=self.draw(rows))
            return
        if payload.startswith("tour:"):
            text, rows = tour_page("max", payload)
            # The answer replaces the tour message: no new messages per step.
            await self.client.answer_callback(callback.get("callback_id"), text=text, buttons=self.draw(rows), format=None)
            return
        if payload == "tour_open:reminders":
            user_id = int((callback.get("user") or {}).get("user_id"))
            preference = await update_reminder_preference(db, "max", user_id, "platform")
            await self.client.answer_callback(callback.get("callback_id"), notification="Настройки напоминаний")
            await self.client.send_message(preference_text(preference), user_id=user_id, buttons=self.reminder_panel(preference))
            return
        if payload in {"summary_switch:today", "summary_switch:week"}:
            # Under a summary: replace it in place.
            user_id = int((callback.get("user") or {}).get("user_id"))
            kind = payload.split(":", 1)[1]
            await self.client.answer_callback(
                callback.get("callback_id"), text=await self.summary_text(db, user_id, kind),
                buttons=self.summary_buttons(kind),
            )
            return
        if payload in {"summary:today", "summary:week"}:
            user_id = int((callback.get("user") or {}).get("user_id"))
            kind = payload.split(":", 1)[1]
            await self.client.answer_callback(callback.get("callback_id"), notification="План обновлён")
            await self.send_summary(db, user_id, kind)
            return
        if payload.startswith("calimp_undo:"):
            user_id = int((callback.get("user") or {}).get("user_id"))
            text = await undo_imports(db, "max", user_id, payload)
            await self.client.answer_callback(callback.get("callback_id"), notification="Готово")
            await self.client.send_message(text, user_id=user_id, format=None, buttons=[[self.app_button()]])
            return
        if payload == "calendar_feed:reset":
            user_id = int((callback.get("user") or {}).get("user_id"))
            await self.client.answer_callback(callback.get("callback_id"), notification="Старая ссылка отключена")
            await self.send_calendar_feed(user_id, db, reset=True)
            return
        if payload == "usage_help":
            callback_id = callback.get("callback_id")
            user_id = int((callback.get("user") or {}).get("user_id"))
            await self.client.send_message(usage_guide("max"), user_id=user_id)
            await self.client.answer_callback(
                callback_id, notification="Инструкция отправлена"
            )
            return
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
