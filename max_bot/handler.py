import asyncio
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .api import MaxApiClient, callback_button, open_app_button
from .calendar import delete_max_yandex, sync_max_yandex
from conversation_service import event_context_text, remember_dialogue_turn
from chat_edit_service import apply_chat_edit, is_edit_request
from .config import MaxSettings
from .models import MaxEvent, MaxEventTiming, MaxUser
from schedule_ai_service import extract_weekly_schedule
from schedule_document_service import (
    MAX_FILE_BYTES,
    ScheduleDocumentClarification,
    parse_schedule_document,
)
from schedule_service import (
    cancel_import,
    confirm_import,
    create_import_draft,
    finish_import_source,
    import_preview,
    parse_date_range,
    parse_occurrence_date,
    pending_draft,
    pending_import_source,
    save_import_source,
    set_draft_range,
    skip_candidates,
    skip_occurrence,
    update_import_source_prompt,
)
from reminder_service import acknowledge_delivery
from unified_calendar import (
    delete_linked_event,
    entry_is_upcoming,
    find_linked_all_day_overlaps,
    find_linked_conflict,
    get_owned_entry,
)
from .service import CANCEL_RE, MaxEventService, format_interval


logger = logging.getLogger("MaxUpdateHandler")
SKIP_SCHEDULE_RE = re.compile(
    r"^\s*(?:я\s+)?(?:не\s+(?:иду|пойду)|пропущу|пропускаю)\b", re.IGNORECASE
)


class MaxUpdateHandler:
    def __init__(self, client: MaxApiClient, service: MaxEventService, settings: MaxSettings):
        self.client, self.service, self.settings = client, service, settings

    def app_button(self):
        return open_app_button("📅 Открыть календарь", self.settings.miniapp_name or None)

    async def welcome(self, user_id: int) -> None:
        await self.client.send_message(
            "Привет! Я **планиИруй! для MAX**. Напишите или пришлите голосом, например: "
            "«Контрольная по математике завтра с 13 до 14». Я покажу карточку перед добавлением.\n\n"
            "Удаление: «отмена контрольная завтра в 13». Можно также прислать фото или файл "
            "с подписью-инструкцией и периодом расписания.",
            user_id=user_id,
            buttons=[[self.app_button()]],
        )

    async def send_event_card(self, user_id: int, event: MaxEvent, db: AsyncSession, source: str = "") -> None:
        timing = event.timing
        try:
            all_day_overlaps = await find_linked_all_day_overlaps(
                db, "max", user_id, timing.start_at, timing.end_at,
                exclude_ref=f"m:{event.id}",
            )
        except StopAsyncIteration:
            all_day_overlaps = []
        overlap_text = ""
        if all_day_overlaps:
            overlap_rows = "\n".join(
                f"• {item.event.title} — {format_interval(item.timing.start_at, item.timing.end_at, item.timezone_name, all_day=True)}"
                for item in all_day_overlaps[:3]
            )
            suffix = "\n• …" if len(all_day_overlaps) > 3 else ""
            overlap_text = (
                "\n⚠️ У вас есть пересечение с мероприятием на весь день или дольше:\n"
                f"{overlap_rows}{suffix}\nНовое событие всё равно можно добавить.\n"
            )
        reminders = "\n".join(
            f"• {item.remind_at.astimezone():%d.%m в %H:%M}" for item in sorted(event.reminders, key=lambda x: x.remind_at)
        ) or "• Нет будущих напоминаний"
        text = (
            (source + "\n" if source else "") + f"**{event.title}**\n"
            f"🕒 {format_interval(timing.start_at, timing.end_at, all_day=bool(getattr(timing, "all_day", False)))}\n"
            + (f"📍 {event.description}\n" if event.description else "")
            + overlap_text
            + f"\n**Напоминания:**\n{reminders}\n\nДобавить мероприятие в календарь?"
        )
        await self.client.send_message(text, user_id=user_id, buttons=[[
            callback_button("✅ Подтвердить", f"confirm:{event.id}"),
            callback_button("❌ Отменить", f"cancel:{event.id}"),
        ]])
        await remember_dialogue_turn(
            db, "max", user_id,
            event_context_text(
                event.title, timing.start_at, timing.end_at,
                all_day=bool(getattr(timing, "all_day", False)),
            ),
            role="assistant", event_ref=f"m:{event.id}", commit=True,
        )

    async def handle_chat_edit(self, user_id: int, raw_text: str, db: AsyncSession) -> bool:
        if not is_edit_request(raw_text):
            return False
        extracted = await self.service.extract_text_input(db, user_id, raw_text)
        result = await apply_chat_edit(db, "max", user_id, raw_text, extracted)
        await remember_dialogue_turn(
            db, "max", user_id, self.service.anonymizer.anonymize_text(raw_text),
            commit=True,
        )
        if result.status == "updated" and result.entry:
            entry = result.entry
            overlaps = await find_linked_all_day_overlaps(
                db, "max", user_id, entry.timing.start_at, entry.timing.end_at,
                exclude_ref=entry.ref,
            )
            warning = ""
            if overlaps:
                warning = "\n\n⚠️ Есть пересечение с событием на весь день или дольше: " + ", ".join(
                    f"«{item.event.title}»" for item in overlaps[:3]
                ) + "."
            await remember_dialogue_turn(
                db, "max", user_id,
                event_context_text(
                    entry.event.title, entry.timing.start_at, entry.timing.end_at,
                    all_day=bool(getattr(entry.timing, "all_day", False)),
                ),
                role="assistant", event_ref=entry.ref, commit=True,
            )
            await self.client.send_message(
                f"✅ Изменено: «{entry.event.title}» — "
                f"{format_interval(entry.timing.start_at, entry.timing.end_at, entry.timezone_name, all_day=bool(getattr(entry.timing, "all_day", False)))}.{warning}",
                user_id=user_id, buttons=[[self.app_button()]],
            )
            return True
        if result.status == "ambiguous":
            variants = "\n".join(
                f"• {item.event.title} — {format_interval(item.timing.start_at, item.timing.end_at, item.timezone_name, all_day=bool(getattr(item.timing, "all_day", False)))}"
                for item in result.candidates
            )
            await self.client.send_message(
                "Нашёл несколько событий. Уточните название, исходную дату или время:\n" + variants,
                user_id=user_id,
            )
            return True
        if result.status == "draft":
            await self.client.send_message(
                "Это событие ещё ожидает подтверждения. Отмените карточку и отправьте уточнённый вариант.",
                user_id=user_id,
            )
            return True
        if result.status == "past":
            await self.client.send_message(
                "Это мероприятие уже прошло и осталось только в истории календаря. Для изменения укажите будущее мероприятие.",
                user_id=user_id,
            )
            return True
        if result.status == "missing":
            await self.client.send_message(
                "Не нашёл, какое событие изменить. Назовите его, например: «перенеси контрольную завтра в 10».",
                user_id=user_id,
            )
            return True
        await self.client.send_message("Не удалось понять, что изменить. Укажите новое время или дату.", user_id=user_id)
        return True

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

    @staticmethod
    def attachment_url(attachment: dict) -> str | None:
        payload = attachment.get("payload") or {}
        return (
            payload.get("url")
            or payload.get("download_url")
            or attachment.get("url")
        )

    @staticmethod
    def attachment_name(attachment: dict) -> str:
        payload = attachment.get("payload") or {}
        name = str(
            payload.get("name")
            or payload.get("file_name")
            or payload.get("filename")
            or ("schedule.jpg" if attachment.get("type") == "image" else "document")
        )
        if "." not in name:
            mime = str(
                payload.get("mime_type") or payload.get("content_type") or ""
            ).casefold()
            suffix = {
                "application/pdf": ".pdf",
                "application/vnd.ms-excel": ".xls",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                "text/csv": ".csv",
                "text/plain": ".txt",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                "image/png": ".png",
                "image/jpeg": ".jpg",
            }.get(mime, "")
            name += suffix
        return name

    async def processing_status(self, user_id: int, subject: str = "Ваше сообщение") -> None:
        try:
            await self.client.send_message(f"⏳ {subject} обрабатывается…", user_id=user_id)
        except Exception:
            # The status is informational and must never interrupt processing.
            logger.warning("Could not send MAX processing status", exc_info=True)

    @staticmethod
    def verified_preview(draft, extraction: dict) -> str:
        verification = extraction.get("verification") or {}
        source_days = verification.get("source_weekdays") or []
        output_days = verification.get("output_weekdays") or []
        labels = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        checks = []
        if source_days:
            checks.append(
                "Проверка покрытия: "
                + ", ".join(labels[index] for index in output_days)
                + " из источника "
                + ", ".join(labels[index] for index in source_days)
            )
        checks.extend(verification.get("warnings") or [])
        prefix = "✅ Источник проверен."
        if checks:
            prefix += "\n" + "\n".join(checks)
        return prefix + "\n\n" + import_preview(draft)

    async def create_verified_schedule_draft(
        self,
        db: AsyncSession,
        user_id: int,
        content: bytes,
        filename: str,
        prompt: str,
        date_range=None,
    ):
        extraction = await parse_schedule_document(
            content, filename, prompt, valid_range=date_range
        )
        for slot in extraction["slots"]:
            slot["title"] = self.service.anonymizer.clean_event_title(
                self.service.anonymizer.anonymize_text(slot.get("title", ""))
            )
            slot["description"] = self.service.anonymizer.clean_display_text(
                self.service.anonymizer.anonymize_text(slot.get("description", ""))
            )
        draft = await create_import_draft(db, "max", user_id, extraction)
        if date_range and getattr(draft, "status", "awaiting_range") == "awaiting_range":
            await set_draft_range(db, draft, *date_range)
        return draft, extraction

    async def send_schedule_preview(self, user_id: int, draft, extraction: dict) -> None:
        if getattr(draft, "status", "awaiting_range") == "awaiting_range":
            await self.client.send_message(
                "В файле нет точных дат для повторяющегося расписания. "
                "Укажите период двумя датами, например: «с 1 сентября по 1 декабря».",
                user_id=user_id,
            )
            return
        await self.client.send_message(
            self.verified_preview(draft, extraction),
            user_id=user_id,
            buttons=[
                [callback_button("✅ Добавить расписание", f"schedule_confirm:{draft.id}")],
                [callback_button("❌ Отменить импорт", f"schedule_cancel:{draft.id}")],
            ],
        )

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
        if text and SKIP_SCHEDULE_RE.match(text):
            target = parse_occurrence_date(text)
            if target is None:
                await self.client.send_message(
                    "Укажите дату занятия. Например: «не иду на математику завтра».",
                    user_id=user_id,
                )
                return
            candidates = await skip_candidates(db, "max", user_id, target, text)
            if not candidates:
                await self.client.send_message(
                    "На эту дату не нашёл подходящего занятия в расписании.", user_id=user_id
                )
                return
            if len(candidates) == 1:
                await skip_occurrence(db, "max", user_id, candidates[0]["id"])
                await self.client.send_message(
                    f"Занятие «{candidates[0]['title']}» {target:%d.%m} скрыто в календаре.",
                    user_id=user_id,
                )
                return
            buttons = [[callback_button(
                f"Пропустить {item['start_local']} · {item['title'][:28]}",
                f"skip_schedule:{item['id']}",
            )] for item in candidates[:8]]
            await self.client.send_message("Какое занятие пропустить?", user_id=user_id, buttons=buttons)
            return

        source = await pending_import_source(db, "max", user_id)
        if source:
            if re.search(
                r"\b(?:отмена|отменить)\s+(?:импорт|расписание)\b", text, re.I
            ):
                await finish_import_source(db, source, "cancelled")
                await self.client.send_message("Импорт расписания отменён.", user_id=user_id)
                return
            combined_prompt = " ".join(
                part for part in (source.prompt.strip(), text) if part
            )
            try:
                date_range = parse_date_range(combined_prompt)
            except ValueError as error:
                await self.client.send_message(
                    f"❌ Некорректный период: {error}", user_id=user_id
                )
                return
            await self.processing_status(user_id, "Ваш файл")
            try:
                draft, extraction = await self.create_verified_schedule_draft(
                    db, user_id, source.content, source.filename,
                    combined_prompt, date_range,
                )
                await finish_import_source(db, source)
                await self.send_schedule_preview(user_id, draft, extraction)
            except ScheduleDocumentClarification as clarification:
                await update_import_source_prompt(db, source, combined_prompt)
                await self.client.send_message(f"🔎 {clarification}", user_id=user_id)
            except Exception as error:
                await db.rollback()
                logger.error(
                    "Saved MAX schedule source processing failed: %s", error,
                    exc_info=True,
                )
                await self.client.send_message(f"❌ {error}", user_id=user_id)
            return

        draft = await pending_draft(db, "max", user_id)
        if text and draft and draft.status == "awaiting_range":
            if re.search(r"\b(?:отмена|отменить)\s+(?:импорт|расписание)\b", text, re.I):
                await cancel_import(db, "max", user_id, draft.id)
                await self.client.send_message("Импорт расписания отменён.", user_id=user_id)
                return
            date_range = parse_date_range(text)
            if date_range is None:
                await self.client.send_message(
                    "Для распознанного расписания сначала укажите период, например: "
                    "«с 1 сентября по 1 декабря». Отменить: «отмена импорта».",
                    user_id=user_id,
                )
                return
            await set_draft_range(db, draft, *date_range)
            await self.client.send_message(
                import_preview(draft), user_id=user_id, buttons=[
                    [callback_button("✅ Добавить расписание", f"schedule_confirm:{draft.id}")],
                    [callback_button("❌ Отменить импорт", f"schedule_cancel:{draft.id}")],
                ],
            )
            return
        attachments = body.get("attachments") or []
        try:
            document = next((x for x in attachments if x.get("type") == "file"), None)
            if document:
                payload = document.get("payload") or {}
                size = payload.get("size") or document.get("size")
                if size is not None and int(size) > MAX_FILE_BYTES:
                    await self.client.send_message(
                        "❌ Файл слишком большой. Максимальный размер — 15 МБ.",
                        user_id=user_id,
                    )
                    return
                url = self.attachment_url(document)
                if not url:
                    raise ValueError("MAX не передал ссылку для скачивания файла.")
                await self.processing_status(user_id, "Ваш файл")
                content = await self.client.download(url)
                date_range = parse_date_range(text) if text else None
                filename = self.attachment_name(document)
                draft, extraction = await self.create_verified_schedule_draft(
                    db, user_id, content, filename, text, date_range
                )
                await self.send_schedule_preview(user_id, draft, extraction)
                return

            image = next((x for x in attachments if x.get("type") == "image"), None)
            image_content = None
            if image:
                await self.processing_status(user_id, "Ваше изображение")
                image_url = self.attachment_url(image)
                if not image_url:
                    raise ValueError("MAX не передал ссылку на изображение.")
                image_content = await self.client.download(image_url)
                date_range = parse_date_range(text) if text else None
                if text:
                    draft, extraction = await self.create_verified_schedule_draft(
                        db, user_id, image_content, self.attachment_name(image),
                        text, date_range,
                    )
                    await self.send_schedule_preview(user_id, draft, extraction)
                    return
                weekly = await extract_weekly_schedule(image_content)
                if weekly:
                    draft, extraction = await self.create_verified_schedule_draft(
                        db, user_id, image_content, self.attachment_name(image),
                        "", None,
                    )
                    await self.send_schedule_preview(user_id, draft, extraction)
                    return
            if text and not text.startswith("/") and not attachments:
                await self.processing_status(user_id)
                if await self.handle_chat_edit(user_id, text, db):
                    return
                events = await self.service.from_text(db, user_id, text)
                source = ""
            else:
                attachment = next((x for x in attachments if x.get("type") in {"image", "audio"}), None)
                if not attachment:
                    await self.client.send_message("Пришлите текст, голосовое сообщение или изображение расписания.", user_id=user_id)
                    return
                if attachment["type"] == "audio":
                    await self.processing_status(user_id, "Ваше голосовое сообщение")
                content = (
                    image_content
                    if attachment["type"] == "image" and image_content is not None
                    else await self.client.download(self.attachment_url(attachment) or "")
                )
                if attachment["type"] == "image":
                    events = await self.service.from_image(
                        db, user_id, image_content if image_content is not None else content
                    )
                    source = "🖼 Распознано из изображения"
                else:
                    transcript = await self.service.speechkit.transcribe_voice(content)
                    if await self.handle_chat_edit(user_id, transcript, db):
                        return
                    events = await self.service.from_text(db, user_id, transcript)
                    source = "🎙 Распознано из голосового сообщения"
            if not events:
                await self.client.send_message("Не удалось найти мероприятие. Укажите дату и время точнее.", user_id=user_id)
            for event in events:
                await self.send_event_card(user_id, event, db, source)
        except ScheduleDocumentClarification as clarification:
            if document:
                await save_import_source(db, "max", user_id, content, filename, text)
            elif image and image_content is not None:
                await save_import_source(
                    db, "max", user_id, image_content, self.attachment_name(image), text
                )
            await self.client.send_message(f"🔎 {clarification}", user_id=user_id)
        except Exception as error:
            await db.rollback()
            logger.error("MAX input processing failed: %s", error, exc_info=True)
            await self.client.send_message(f"❌ Не удалось обработать сообщение: {error}", user_id=user_id)

    async def handle_cancellation(self, user_id: int, text: str, db: AsyncSession) -> None:
        if not CANCEL_RE.match(text).group(1).strip():
            await self.client.send_message("Напишите, что удалить. Например: «отмена контрольная по русскому».", user_id=user_id)
            return
        candidates = await self.service.cancellation_candidates(db, user_id, text)
        await remember_dialogue_turn(
            db, "max", user_id, self.service.anonymizer.anonymize_text(text),
            commit=True,
        )
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
            if action == "schedule_confirm":
                state, created = await confirm_import(db, "max", user_id, int(event_text))
                if state == "missing":
                    await self.client.answer_callback(callback_id, notification="Черновик не найден")
                elif state == "not_ready":
                    await self.client.answer_callback(callback_id, notification="Сначала укажите период")
                elif state == "imported":
                    await self.client.answer_callback(callback_id, notification="Уже добавлено")
                else:
                    await self.client.answer_callback(
                        callback_id,
                        text=f"✅ Расписание добавлено: {created} занятий в недельном шаблоне.\n"
                        "Оно показано фоном и не блокирует личные мероприятия.",
                        notification="Расписание добавлено",
                    )
                return
            if action == "schedule_cancel":
                cancelled = await cancel_import(db, "max", user_id, int(event_text))
                await self.client.answer_callback(
                    callback_id,
                    text="Импорт расписания отменён. Календарь не изменён." if cancelled else None,
                    notification="Импорт отменён" if cancelled else "Черновик уже закрыт",
                )
                return
            if action == "skip_schedule":
                skipped = await skip_occurrence(db, "max", user_id, event_text)
                await self.client.answer_callback(
                    callback_id,
                    text="Занятие скрыто в календаре только на выбранную дату." if skipped else None,
                    notification="Занятие скрыто" if skipped else "Занятие не найдено",
                )
                return
            if action == "delete" and ":" in event_text:
                entry = await get_owned_entry(db, "max", user_id, event_text)
                if not entry:
                    await self.client.answer_callback(callback_id, notification="Событие уже удалено")
                    return
                if not entry_is_upcoming(entry):
                    await self.client.answer_callback(callback_id, notification="Прошедшее событие доступно только в истории календаря")
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
                await self.client.answer_callback(callback_id, text=f"🗑 {event.title}\n{format_interval(timing.start_at, timing.end_at, all_day=bool(getattr(timing, "all_day", False)))}\nУдалено.", notification="Удалено")
                return
            if action == "complete":
                row = await self.owned(db, user_id, event_id)
                if not row:
                    await self.client.answer_callback(callback_id, notification="Событие не найдено")
                    return
                row[0].is_completed = not row[0].is_completed
                if row[0].is_completed:
                    await acknowledge_delivery(db, "max", row[0].id, user_id)
                else:
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
            local_conflict = None
            if not bool(getattr(timing, "all_day", False)):
                conflict_result = await db.execute(
                    select(MaxEvent, MaxEventTiming).join(MaxEventTiming).filter(
                        MaxEvent.user_id == event.user_id, MaxEvent.id != event.id,
                        MaxEvent.status == "confirmed", MaxEventTiming.all_day.is_(False),
                        MaxEventTiming.start_at < timing.end_at,
                        MaxEventTiming.end_at > timing.start_at,
                    )
                )
                local_conflict = conflict_result.first()
            linked_conflict = None
            if not local_conflict:
                try:
                    linked_conflict = await find_linked_conflict(
                        db, "max", user_id, timing.start_at, timing.end_at,
                        exclude_ref=f"m:{event.id}", all_day=bool(getattr(timing, "all_day", False)),
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
                    text=f"⚠️ Мероприятия перекрываются.\nУже запланировано: «{other.title}» — {format_interval(other_time.start_at, other_time.end_at, all_day=bool(getattr(other_time, "all_day", False)))}.\n\nНовое мероприятие не добавлено.",
                    notification="Мероприятия перекрываются",
                )
                return
            if event.status != "confirmed":
                event.status = "confirmed"
                await db.commit()
                asyncio.create_task(sync_max_yandex(event.title, timing.start_at, timing.end_at, event.description or "", event.id, all_day=bool(getattr(timing, "all_day", False))))
            await self.client.answer_callback(callback_id, text=f"✅ {event.title}\n{format_interval(timing.start_at, timing.end_at, all_day=bool(getattr(timing, "all_day", False)))}\nДобавлено в календарь.", notification="Подтверждено")
        except Exception as error:
            await db.rollback()
            logger.error("MAX callback failed: %s", error, exc_info=True)
            await self.client.answer_callback(callback_id, notification="Не удалось выполнить действие")
