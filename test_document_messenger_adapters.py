from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.pipeline_handlers_intervals import processing_status
from max_bot.handler import MaxUpdateHandler


@pytest.mark.anyio
async def test_telegram_processing_status_uses_complete_readable_phrase():
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock(return_value=status))

    result = await processing_status(message, "Ваш файл")

    assert result is status
    message.answer.assert_awaited_once_with("⏳ Ваш файл обрабатывается…")


@pytest.mark.anyio
async def test_max_file_is_processed_with_prompt_and_visible_status():
    client = SimpleNamespace(
        send_message=AsyncMock(),
        download=AsyncMock(return_value=b"spreadsheet"),
    )
    anonymizer = SimpleNamespace(
        anonymize_text=lambda value: value,
        clean_event_title=lambda value: value,
        clean_display_text=lambda value: value,
    )
    service = SimpleNamespace(anonymizer=anonymizer)
    handler = MaxUpdateHandler(
        client, service, SimpleNamespace(miniapp_name="planiruy")
    )
    db = MagicMock(spec=AsyncSession)
    extraction = {
        "confidence": 0.95,
        "chunks_processed": 2,
        "verification": {"source_weekdays": [3], "output_weekdays": [3]},
        "slots": [{
            "title": "Смена", "weekday": 3, "occurrence_date": "2026-09-03",
            "start_time": "09:00", "end_time": "18:00", "description": "",
            "week_pattern": "every", "confidence": 0.95,
        }],
    }
    draft = SimpleNamespace(id=17)

    with patch("max_bot.handler.parse_schedule_document", new_callable=AsyncMock, return_value=extraction) as parse, \
         patch("max_bot.handler.pending_import_source", new_callable=AsyncMock, return_value=None), \
         patch("max_bot.handler.pending_draft", new_callable=AsyncMock, return_value=None), \
         patch("max_bot.handler.create_import_draft", new_callable=AsyncMock, return_value=draft), \
         patch("max_bot.handler.set_draft_range", new_callable=AsyncMock), \
         patch("max_bot.handler.import_preview", return_value="Предпросмотр"):
        await handler.handle_message({
            "sender": {"user_id": 321},
            "body": {
                "text": "выбери смены Анны с 1 сентября по 1 декабря",
                "attachments": [{
                    "type": "file",
                    "payload": {
                        "url": "https://files.max.ru/schedule.xlsx",
                        "name": "schedule.xlsx",
                        "size": 100,
                    },
                }],
            },
        }, db)

    first = client.send_message.await_args_list[0]
    assert first.args[0] == "⏳ Ваш файл обрабатывается…"
    assert client.send_message.await_args_list[-1].args[0] == (
        "✅ Источник проверен.\nПроверка покрытия: Чт из источника Чт\n\nПредпросмотр"
    )
    parse.assert_awaited_once_with(
        b"spreadsheet", "schedule.xlsx",
        "выбери смены Анны с 1 сентября по 1 декабря",
        valid_range=(date(2026, 9, 1), date(2026, 12, 1)),
    )
