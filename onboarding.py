"""A one-minute tour of the bot. The same steps for Telegram and MAX.

Buttons are described neutrally — ("callback", text, data), ("copy", text, value)
or ("app", text, "") — and each messenger draws them with its own button types.
"""

EXAMPLE = "завтра в 15:00 встреча с куратором"


def commands_text(platform: str) -> str:
    """The command list shown on the very first start."""
    calendar = (
        "/calendar — календарь (или кнопка «Календарь» слева от поля ввода)"
        if platform == "telegram" else "/calendar — календарь и ближайшие дела"
    )
    return "\n".join([
        "📖 Команды",
        "/tour — обучение за минуту",
        "/today — план на сегодня",
        "/week — план на 7 дней",
        calendar,
        "/history — всё добавленное, с удалением",
        "/reminders — напоминания и утренняя сводка",
        "/import — загрузить из другого календаря",
        "/calendars — мои события в Apple, Google, Яндекс",
        "/link — общий календарь Telegram и MAX",
        "/help — подробная справка",
    ])


def tour_steps(platform: str) -> list[str]:
    calendar_hint = (
        "кнопкой «Календарь» слева от поля ввода" if platform == "telegram"
        else "кнопкой «Открыть календарь» под сообщениями"
    )
    return [
        "👋 Знакомство за минуту\n\n"
        "Я — календарь, в который пишут обычными словами. Покажу главное за 5 коротких "
        "шагов. Пропустить можно в любой момент.",

        "✏️ Шаг 1 из 5. Пишите как другу\n\n"
        "Например:\n"
        "• «завтра в 15:00 встреча с куратором»\n"
        "• «в пятницу с 10 до 12 лабораторная»\n"
        "• «отпуск с 1 по 7 ноября»\n\n"
        "Я покажу карточку: проверьте дату и нажмите «Подтвердить». Перенести: "
        "«перенеси встречу на 16:00». Удалить: «отмени встречу с куратором».\n\n"
        "Скопируйте пример кнопкой ниже и отправьте мне — посмотрите, как это работает.",

        "🎙 Шаг 2 из 5. Голос, фото и файлы\n\n"
        "• Голосовое — скажите то же, что написали бы.\n"
        "• Фото расписания или файл Excel, PDF, Word — соберу недельное расписание и "
        "спрошу период, например «с 1 сентября по 31 декабря».\n"
        "• Если в файле несколько групп, укажите свою в подписи: «ИВТ-22».\n"
        "• Файл .ics или ссылка на другой календарь — события загрузятся сами.",

        "📅 Шаг 3 из 5. Календарь\n\n"
        f"Откройте календарь {calendar_hint}. В нём день, неделя (на компьютере) и список "
        "дел. События можно перетаскивать, растягивать и редактировать. На вкладке "
        "«История» — всё, что вы добавляли; импорт расписания удаляется целиком.",

        "🔔 Шаг 4 из 5. Напоминания и сводки\n\n"
        "Напомню заранее — время подберу по смыслу события. Каждое утро могу присылать "
        "план на день.\n"
        "• /today — что сегодня\n"
        "• /week — план на 7 дней, свободные дни и пересечения\n"
        "• /reminders — когда и как напоминать",

        "📆 Шаг 5 из 5. Другие календари\n\n"
        "• /calendars — показывать мои события в Apple, Google или Яндекс Календаре.\n"
        "• /import — загрузить события из другого календаря (.ics, ссылка, CalDAV).\n"
        "• /link — один календарь на Telegram и MAX.",
    ]


DONE_TEXT = (
    "✅ Готово! Коротко:\n\n"
    "• Пишите или говорите, что и когда, — я добавлю в календарь.\n"
    "• /today и /week — сводки на день и неделю.\n"
    "• /history — всё добавленное, с удалением.\n"
    "• /help — подробная справка, /tour — это обучение ещё раз."
)


def tour_buttons(step: int) -> list[list[tuple[str, str, str]]]:
    """Rows of neutral buttons for a step; step == len(steps) is the final message."""
    last = len(tour_steps("telegram")) - 1
    if step > last:
        return [
            [("app", "📅 Открыть календарь", "")],
            [("callback", "📊 План на неделю", "summary:week")],
        ]
    extra = {
        1: [[("copy", "✏️ Скопировать пример", EXAMPLE)]],
        3: [[("app", "📅 Открыть календарь", "")]],
        4: [[("callback", "🔔 Настроить напоминания", "tour_open:reminders")],
            [("callback", "📊 План на неделю", "summary:week")]],
    }.get(step, [])
    if step == 0:
        navigation = [[("callback", "➡️ Начать", "tour:1")], [("callback", "Пропустить", "tour:end")]]
    elif step == last:
        navigation = [[("callback", "⬅️ Назад", f"tour:{step - 1}"), ("callback", "✅ Готово", "tour:end")]]
    else:
        navigation = [
            [("callback", "⬅️ Назад", f"tour:{step - 1}"), ("callback", "➡️ Дальше", f"tour:{step + 1}")],
            [("callback", "Пропустить", "tour:end")],
        ]
    return extra + navigation


def first_run_buttons() -> list[list[tuple[str, str, str]]]:
    """The first tour step on the very first start, with the calendar at hand."""
    return tour_buttons(0) + [[("app", "📅 Открыть календарь", "")]]


def tour_page(platform: str, data: str) -> tuple[str, list[list[tuple[str, str, str]]]]:
    """Text and buttons for callback data «tour:<n>», «tour:start» (from the welcome) or «tour:end»."""
    steps = tour_steps(platform)
    value = data.split(":", 1)[1] if ":" in data else "0"
    if value == "end":
        return DONE_TEXT, tour_buttons(len(steps))
    try:
        step = min(max(int(value), 0), len(steps) - 1)
    except ValueError:
        step = 0
    return steps[step], tour_buttons(step)
