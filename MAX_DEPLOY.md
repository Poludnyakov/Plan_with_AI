# Развёртывание «планиИруй!» в MAX

MAX работает отдельным контейнером `planiruy_max` на `127.0.0.1:8001`. Telegram остаётся на
`127.0.0.1:8000`, другой сайт и его nginx-блок менять не нужно. Данные MAX лежат в отдельных
таблицах `max_*`, но используется тот же PostgreSQL и те же ключи Yandex Cloud.

## 1. Создание бота и Mini App

1. В профиле для бизнеса MAX создайте чат-бота, дождитесь модерации и скопируйте токен.
2. В расширенных настройках бота создайте Mini App и укажите URL
   `https://planwithai.ru/max/miniapp`. Если MAX выдаст короткое имя приложения, сохраните его
   как `MAX_MINIAPP_NAME`; иначе оставьте переменную пустой.
3. Не публикуйте токен или webhook secret. Если токен когда-либо попал в чат/репозиторий,
   перевыпустите его в MAX.

## 2. Файлы и переменные

Копируется весь проект, включая `max_bot/` и `docker-compose.max.yml`. На сервере:

```bash
cd /home/planiruy
nano planiruy.env
```

Добавьте в существующий `planiruy.env`:

```dotenv
MAX_BOT_TOKEN=ТОКЕН_ИЗ_MAX
MAX_WEBHOOK_SECRET=СЛУЧАЙНАЯ_СТРОКА_64_СИМВОЛА
MAX_PUBLIC_BASE_URL=https://planwithai.ru
MAX_MINIAPP_NAME=
MAX_BOT_USERNAME=НИК_ИЗ_ССЫЛКИ_MAX_БЕЗ_СОБАКИ
MAX_API_BASE_URL=https://platform-api2.max.ru
MAX_AUTO_REGISTER_WEBHOOK=true
```

Секрет можно получить без сохранения истории: `openssl rand -hex 32`, затем сразу вставить в
редактор. Защитите env:

```bash
chmod 600 /home/planiruy/planiruy.env
```

## 3. Маршрутизация nginx

В существующий HTTPS-блок `server` для **planwithai.ru** (не в конфигурацию другого домена)
добавьте перед общим `location /`:

```nginx
location /max/ {
    proxy_pass http://127.0.0.1:8001;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
```

У `proxy_pass` здесь намеренно нет завершающего `/`: backend должен получить путь `/max/...`.
Проверьте и перечитайте конфигурацию без остановки сайтов:

```bash
nginx -t
systemctl reload nginx
```

## 4. Сборка и запуск

Команда подключает базовый compose (PostgreSQL/Telegram) и только добавляет MAX-сервис:

```bash
cd /home/planiruy
docker compose \
  -f docker-compose.yml \
  -f docker-compose.intervals.yml \
  -f docker-compose.max.yml \
  --env-file /home/planiruy/planiruy.env \
  up -d --build db app bot max_app
```

При старте MAX-сервис создаст только недостающие таблицы, установит команды и зарегистрирует
webhook `https://planwithai.ru/max/webhook`. Существующие таблицы и данные не удаляются.

## 5. Проверка

```bash
docker compose \
  -f docker-compose.yml -f docker-compose.intervals.yml -f docker-compose.max.yml \
  --env-file /home/planiruy/planiruy.env ps

curl -fsS http://127.0.0.1:8001/max/health
curl -fsS https://planwithai.ru/max/health

docker compose \
  -f docker-compose.yml -f docker-compose.intervals.yml -f docker-compose.max.yml \
  --env-file /home/planiruy/planiruy.env logs --tail=150 max_app
```

Ожидаемый health-ответ: `{"status":"ok","service":"planiruy-max"}`. Затем в MAX:

1. нажмите «Начать» и проверьте приветствие;
2. напишите `контрольная по математике завтра с 13 до 14`;
3. подтвердите карточку и откройте календарь;
4. попробуйте добавить пересекающееся событие — оно должно быть отклонено;
5. напишите `отмена контрольная завтра в 13`;
6. отправьте голосовое и изображение расписания.

Если автоматическая регистрация webhook не прошла, сначала смотрите лог. После исправления сети
её можно безопасно повторить:

```bash
docker compose \
  -f docker-compose.yml -f docker-compose.intervals.yml -f docker-compose.max.yml \
  --env-file /home/planiruy/planiruy.env \
  exec max_app python -m max_bot.register_webhook
```

## Обновления в дальнейшем

Правило проекта записано в `AGENTS.md`: продуктовые изменения по умолчанию реализуются и
проверяются одновременно для Telegram и MAX. Исключение — когда задача явно ограничена одной
платформой или функция объективно платформенная.
