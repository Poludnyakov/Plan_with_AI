import logging
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import User, Event, EventStatus, ReminderStatus
from repositories import EventRepository, ReminderRepository
from config import settings

logger = logging.getLogger("DashboardRouter")

router = APIRouter(tags=["Dashboard"])

# Инициализируем шаблоны Jinja2 в модуле роутера
templates = Jinja2Templates(directory="templates")

# Кэш для имени бота, чтобы не запрашивать API при каждом обращении
_bot_username_cache = None


async def get_bot_username() -> str:
    """
    Асинхронно запрашивает информацию о боте из Telegram API, чтобы получить его username.
    Использует кэширование, чтобы избежать избыточных сетевых вызовов.
    В случае ошибки или отсутствия токена возвращает дефолтное имя: 'plan_with_AI_mipt_bot'.
    """
    global _bot_username_cache
    if _bot_username_cache is not None:
        return _bot_username_cache
    
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return "plan_with_AI_mipt_bot"
        
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    _bot_username_cache = data["result"]["username"]
                    return _bot_username_cache
    except Exception as e:
        logger.warning(f"Не удалось динамически получить имя бота от Telegram API: {e}")
        
    return "plan_with_AI_mipt_bot"


def get_cookie_secret() -> str:
    """
    Возвращает секретный ключ для подписи сессионных кук.
    Использует BOT_TOKEN или надежный резервный ключ для тестового окружения.
    """
    return settings.TELEGRAM_BOT_TOKEN or "fallback_secret_key_for_testing_12345"


def sign_tg_id(user_tg_id: int, secret_key: str) -> str:
    """
    Создает криптографически подписанную сессионную куку на основе Telegram ID пользователя.
    
    Формат: user_tg_id.signature
    Подпись вычисляется с помощью HMAC-SHA256 для защиты от модификации данных на стороне клиента.
    """
    message = str(user_tg_id).encode("utf-8")
    sig = hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{user_tg_id}.{sig}"


def verify_tg_id(signed_cookie: str, secret_key: str) -> Optional[int]:
    """
    Проверяет валидность криптографической подписи куки.
    
    В случае совпадения подписей возвращает исходный user_tg_id в формате int.
    В случае несовпадения подписи или неверного формата возвращает None.
    """
    if not signed_cookie or "." not in signed_cookie:
        return None
    try:
        parts = signed_cookie.split(".", 1)
        if len(parts) != 2:
            return None
        tg_id_str, sig = parts
        expected_sig = hmac.new(
            secret_key.encode("utf-8"), 
            tg_id_str.encode("utf-8"), 
            hashlib.sha256
        ).hexdigest()
        
        if hmac.compare_digest(sig, expected_sig):
            return int(tg_id_str)
    except Exception as e:
        logger.error(f"Ошибка при валидации подписи куки сессии: {e}")
    return None


def verify_telegram_auth(auth_data: dict, bot_token: str) -> bool:
    """
    Криптографическая проверка подписи данных от виджета авторизации Telegram.
    
    Согласно официальному алгоритму Telegram:
    1. Исключаем параметр 'hash' из списка проверяемых полей.
    2. Все переданные параметры сортируются по алфавиту.
    3. Из отсортированных параметров собирается строка проверки данных (data_check_string)
       в виде пар key=value, разделенных символом новой строки '\\n'.
    4. Создается секретный ключ (secret_key), который является бинарным хэшем SHA256
       от текстового токена бота.
    5. Строка проверки данных подписывается алгоритмом HMAC-SHA256 с использованием secret_key.
    6. Полученная шестнадцатеричная строка сравнивается с хэшем, присланным Telegram (параметр hash).
       Сравнение выполняется безопасной функцией hmac.compare_digest для защиты от атак по времени.
    7. Дополнительно проверяется время авторизации (auth_date): оно должно быть не старее 24 часов
       для защиты от атак повторного воспроизведения (replay attacks).
    """
    # 1. Проверяем наличие хэша в переданных параметрах
    received_hash = auth_data.get("hash")
    if not received_hash:
        logger.warning("Проверка Telegram Auth не удалась: отсутствует параметр 'hash'")
        return False

    # 2. Формируем список пар key=value, исключая параметр hash
    data_check_list = []
    for key, value in auth_data.items():
        if key != "hash" and value is not None:
            data_check_list.append(f"{key}={value}")
            
    # Сортируем параметры по алфавиту (требование Telegram)
    data_check_list.sort()
    
    # Объединяем строки через символ новой строки
    data_check_string = "\n".join(data_check_list)
    
    # 3. Вычисляем секретный ключ как SHA256 хэш от токена бота (в бинарном виде)
    if not bot_token:
        logger.error("Проверка Telegram Auth невозможна: отсутствует токен бота в настройках приложения")
        return False
        
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    
    # 4. Вычисляем ожидаемый HMAC-SHA256 хэш
    expected_hash = hmac.new(
        secret_key, 
        data_check_string.encode("utf-8"), 
        hashlib.sha256
    ).hexdigest()
    
    # 5. Сравниваем хэши безопасным образом для защиты от timing attacks
    if not hmac.compare_digest(received_hash, expected_hash):
        logger.warning("Проверка Telegram Auth не удалась: несовпадение хэшей!")
        return False
        
    # 6. Проверяем время авторизации (auth_date), чтобы избежать replay атак
    auth_date = auth_data.get("auth_date")
    if auth_date:
        try:
            auth_timestamp = int(auth_date)
            current_timestamp = int(datetime.now(timezone.utc).timestamp())
            # Разрешаем расхождение во времени до 24 часов (86400 секунд)
            if abs(current_timestamp - auth_timestamp) > 86400:
                logger.warning(f"Проверка Telegram Auth не удалась: устаревшие данные (auth_date={auth_timestamp})")
                return False
        except ValueError:
            logger.warning("Проверка Telegram Auth не удалась: некорректный формат auth_date")
            return False
            
    return True


async def get_current_user_tg_id(request: Request) -> int:
    """
    Зависимость FastAPI для извлечения Telegram ID пользователя из сессионной куки.
    
    Проверяет куку 'planiruy_session' и валидирует её подпись.
    В случае неудачи выбрасывает HTTPException 401.
    """
    cookie_val = request.cookies.get("planiruy_session")
    if not cookie_val:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия не найдена. Требуется авторизация."
        )
    
    cookie_secret = get_cookie_secret()
    user_tg_id = verify_tg_id(cookie_val, cookie_secret)
    if user_tg_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительная или устаревшая сессия. Требуется повторный вход."
        )
        
    return user_tg_id


# ==========================================
# AUTH ENDPOINTS
# ==========================================

@router.get("/login", response_class=HTMLResponse, summary="Render login page with Telegram Widget")
async def get_login_page(request: Request):
    """
    Отрисовывает страницу авторизации templates/login.html.
    Передает динамически полученное имя бота в контекст шаблона.
    """
    bot_name = await get_bot_username()
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "bot_username": bot_name
        }
    )


@router.get("/api/auth/telegram", summary="Telegram Authentication Callback")
async def telegram_auth_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Эндпоинт обратного вызова от виджета Telegram Login.
    Выполняет валидацию криптографической подписи, создает пользователя в БД при необходимости,
    устанавливает защищенную signed-куку сессии и делает редирект на /calendar.
    """
    auth_data = dict(request.query_params)
    
    # 1. Проверяем подпись Telegram Widget
    # Если токен заглушен для тестов и мы находимся в тестовом режиме, можем разрешить mock
    is_testing = settings.TELEGRAM_BOT_TOKEN == "MOCK_TOKEN" or not settings.TELEGRAM_BOT_TOKEN
    
    if not is_testing:
        if not verify_telegram_auth(auth_data, settings.TELEGRAM_BOT_TOKEN):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Недействительная подпись данных Telegram. Авторизация отклонена."
            )
    else:
        logger.info("Проверка Telegram Auth пропущена: активен тестовый/mock режим.")

    # 2. Извлекаем Telegram ID пользователя
    tg_id_str = auth_data.get("id")
    if not tg_id_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Отсутствует Telegram ID в данных авторизации."
        )
        
    try:
        user_tg_id = int(tg_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Некорректный формат Telegram ID."
        )

    # 3. Находим пользователя или автоматически регистрируем его
    user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
    user = user_result.scalar_one_or_none()
    
    if not user:
        # Автоматическая бесшовная регистрация нового студента
        user = User(
            tg_id=user_tg_id,
            timezone="Europe/Moscow"
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info(f"Создан новый пользователь на основе авторизации Telegram виджета: tg_id={user_tg_id}")
    
    # 4. Формируем signed-куку сессии
    cookie_secret = get_cookie_secret()
    signed_cookie = sign_tg_id(user_tg_id, cookie_secret)
    
    # 5. Возвращаем Redirect на страницу календаря
    response = RedirectResponse(url="/calendar", status_code=status.HTTP_303_SEE_OTHER)
    
    # Записываем защищенную, зашифрованную/подписанную сессионную куку
    response.set_cookie(
        key="planiruy_session",
        value=signed_cookie,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=30 * 86400  # 30 дней
    )
    
    logger.info(f"Пользователь tg_id={user_tg_id} успешно авторизован. Сессия установлена.")
    return response


# ==========================================
# PROTECTED DASHBOARD & CALENDAR ENDPOINTS
# ==========================================

@router.get("/dashboard", response_class=HTMLResponse, summary="Render student schedule dashboard")
async def get_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Отрисовывает интерактивный веб-дашборд студента со списком подтвержденных дедлайнов.
    Извлекает Telegram ID напрямую из сессионной куки текущего запроса.
    При невалидной сессии принудительно редиректит на /login.
    """
    try:
        user_tg_id = await get_current_user_tg_id(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        
    try:
        # 1. Извлекаем пользователя из БД
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            logger.warning(f"Дашборд запрошен для несуществующего Telegram ID из сессии: {user_tg_id}")
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

        # 2. Получаем подтвержденные дедлайны
        events_result = await db.execute(
            select(Event)
            .filter(Event.user_id == user.id, Event.status == EventStatus.CONFIRMED)
            .order_by(Event.deadline.asc())
        )
        events = events_result.scalars().all()
        
        logger.info(f"Загружено {len(events)} подтвержденных дедлайнов для пользователя tg_id={user_tg_id}")
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "request": request,
                "user": user,
                "user_tg_id": user_tg_id,
                "events": events
            }
        )
    except Exception as e:
        logger.error(f"Ошибка отрисовки дашборда для tg_id={user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка при загрузке дашборда."
        )


@router.post("/events/{event_id}/toggle-complete", summary="Toggle event completion state")
async def toggle_event_complete(event_id: int, db: AsyncSession = Depends(get_db)):
    """
    Изменяет статус выполнения (is_completed) конкретного события в базе данных.
    Доступен по AJAX без прямой передачи tg_id в пути (проверяется ID события).
    """
    try:
        event_result = await db.execute(select(Event).filter(Event.id == event_id))
        event = event_result.scalar_one_or_none()
        
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Событие с ID {event_id} не найдено."
            )
            
        # Меняем статус
        event.is_completed = not event.is_completed
        await db.commit()
        await db.refresh(event)
        
        logger.info(f"Событие ID {event_id} изменено: is_completed={event.is_completed}")
        return {
            "status": "success",
            "event_id": event.id,
            "is_completed": event.is_completed
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка изменения статуса выполнения события ID {event_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось обновить статус события."
        )


@router.delete("/api/events/{event_id}", summary="Delete an event manually")
async def delete_event(event_id: int, db: AsyncSession = Depends(get_db)):
    """
    Мануально удаляет событие из базы данных.
    Каскадно удаляет связанные запланированные напоминания.
    """
    try:
        event_result = await db.execute(select(Event).filter(Event.id == event_id))
        event = event_result.scalar_one_or_none()
        
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Событие с ID {event_id} не найдено."
            )
            
        event_title = event.title
        await db.delete(event)
        await db.commit()
        
        logger.info(f"Событие ID {event_id} успешно удалено")
        return {
            "status": "success",
            "message": f"Событие '{event_title}' успешно удалено."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка удаления события ID {event_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось удалить событие."
        )


@router.get("/api/events", summary="Get student events in FullCalendar format")
async def get_events_json(
    user_tg_id: int = Depends(get_current_user_tg_id), 
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список всех подтвержденных событий пользователя в JSON формате,
    адаптированном специально для FullCalendar.
    Telegram ID извлекается строго из сессионной куки planiruy_session.
    """
    try:
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Студент с Telegram ID {user_tg_id} не найден."
            )
            
        events_result = await db.execute(
            select(Event)
            .filter(Event.user_id == user.id, Event.status == EventStatus.CONFIRMED)
        )
        events = events_result.scalars().all()
        
        payload = []
        for event in events:
            payload.append({
                "id": event.id,
                "title": event.title,
                "start": event.deadline.isoformat(),
                "description": event.description or "",
                "is_completed": event.is_completed,
                "color": "#4CAF50" if event.is_completed else "#7B2CBF"
            })
            
        logger.info(f"Отправлено {len(payload)} событий для FullCalendar пользователя tg_id={user_tg_id}")
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка отдачи JSON событий для tg_id={user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось загрузить события для календаря."
        )


@router.get("/calendar", response_class=HTMLResponse, summary="Render student schedule visual calendar")
async def get_calendar_page(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Отрисовывает страницу интерактивного FullCalendar (templates/calendar.html).
    Telegram ID пользователя берется напрямую из сессионной куки текущего запроса.
    При невалидной сессии принудительно редиректит на /login.
    """
    try:
        user_tg_id = await get_current_user_tg_id(request)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        
    try:
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            logger.warning(f"Страница календаря запрошена для несуществующего Telegram ID из сессии: {user_tg_id}")
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

        logger.info(f"Отрисовка страницы FullCalendar для пользователя tg_id={user_tg_id} на основе кук")
        return templates.TemplateResponse(
            request=request,
            name="calendar.html",
            context={
                "request": request,
                "user": user,
                "user_tg_id": user_tg_id
            }
        )
    except Exception as e:
        logger.error(f"Ошибка рендеринга страницы календаря для tg_id={user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка при загрузке календаря."
        )


class EventManualCreate(BaseModel):
    title: str = Field(..., max_length=255)
    description: Optional[str] = None
    deadline: datetime


@router.post("/api/events", summary="Create a new event manually with automatic reminders and calendar sync")
async def create_event_manually(
    event_in: EventManualCreate,
    user_tg_id: int = Depends(get_current_user_tg_id),
    db: AsyncSession = Depends(get_db)
):
    """
    Мануально создает подтвержденный дедлайн для авторизованного пользователя,
    автоматически рассчитывает 5 напоминаний и отправляет задачу на синхронизацию CalDAV в Yandex.
    """
    try:
        # Получаем пользователя
        user_result = await db.execute(select(User).filter(User.tg_id == user_tg_id))
        user = user_result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Студент с Telegram ID {user_tg_id} не найден."
            )
            
        # Репозитории для работы с БД
        event_repo = EventRepository(db)
        reminder_repo = ReminderRepository(db)
        
        # Проверяем конфликты времени
        conflicting_event = await event_repo.get_conflicting_event(
            user_id=user.id,
            deadline=event_in.deadline
        )
        if conflicting_event:
            display_time = conflicting_event.deadline.strftime('%d.%m.%Y в %H:%M')
            try:
                import pytz
                if user.timezone:
                    tz = pytz.timezone(user.timezone)
                    display_deadline = conflicting_event.deadline
                    if display_deadline.tzinfo is not None:
                        display_deadline = display_deadline.astimezone(tz)
                    else:
                        display_deadline = pytz.utc.localize(display_deadline).astimezone(tz)
                    display_time = display_deadline.strftime('%d.%m.%Y в %H:%M')
            except Exception:
                pass
                
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Этот временной интервал пересекается с уже забронированной задачей '{conflicting_event.title}' ({display_time})"
            )
        
        db_event = await event_repo.create(
            user_id=user.id,
            title=event_in.title,
            description=event_in.description,
            deadline=event_in.deadline,
            status=EventStatus.CONFIRMED
        )
        await db.flush()
        
        # Рассчитываем и планируем превентивные напоминания
        intervals = [
            timedelta(hours=24),
            timedelta(hours=12),
            timedelta(hours=1),
            timedelta(minutes=30),
            timedelta(minutes=15)
        ]
        now = datetime.now(timezone.utc)
        
        deadline_utc = event_in.deadline
        if deadline_utc.tzinfo is None:
            deadline_utc = deadline_utc.replace(tzinfo=timezone.utc)
        else:
            deadline_utc = deadline_utc.astimezone(timezone.utc)
            
        time_to_deadline = deadline_utc - now
        
        # Если до дедлайна меньше 24 часов, планируем немедленное напоминание
        if time_to_deadline < timedelta(hours=24) and time_to_deadline > timedelta(0):
            await reminder_repo.create(
                event_id=db_event.id,
                remind_at=now,
                status=ReminderStatus.PENDING
            )
            logger.info(f"Запланировано немедленное напоминание для дедлайна {db_event.id}")
            
        for interval in intervals:
            remind_time = deadline_utc - interval
            if remind_time > now:
                await reminder_repo.create(
                    event_id=db_event.id,
                    remind_at=remind_time,
                    status=ReminderStatus.PENDING
                )
                
        await db.commit()
        await db.refresh(db_event)
        
        # Синхронизация с Yandex CalDAV если логин-пароль заданы
        try:
            from yandex_calendar_service import YandexCalendarService
            await YandexCalendarService().add_deadline_to_yandex(
                title=db_event.title,
                deadline=db_event.deadline,
                description=db_event.description
            )
            logger.info(f"Событие {db_event.id} успешно синхронизировано в Яндекс.Календарь")
        except Exception as sync_err:
            logger.warning(f"Не удалось синхронизировать событие в Яндекс.Календарь: {sync_err}", exc_info=True)
            
        logger.info(f"Мануально создан подтвержденный дедлайн {db_event.id} для пользователя tg_id={user_tg_id}")
        return {
            "status": "success",
            "event_id": db_event.id,
            "title": db_event.title,
            "deadline": db_event.deadline.isoformat(),
            "description": db_event.description or ""
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка ручного создания события для tg_id={user_tg_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось создать событие."
        )
