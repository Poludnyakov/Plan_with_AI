# Обновление общего календаря Telegram + MAX

Обновление не удаляет существующие события и не затрагивает второй сайт. При старте SQLAlchemy
создаст только новые таблицы `unified_accounts`, `account_identities`, `account_preferences`,
`account_link_codes` и `web_login_tickets`.

## 1. Переменные окружения

В `/home/planiruy/planiruy.env` должны остаться все прежние значения. Добавьте ник MAX-бота из
его публичной ссылки. Например, для `https://max.ru/id123456_bot`:

```dotenv
MAX_BOT_USERNAME=id123456_bot
```

Это именно ник без `https://max.ru/` и без `@`. Он нужен для кнопки «Войти через MAX» на сайте.
Токены в код или nginx вписывать не нужно.

## 2. Копирование файлов

С компьютера из каталога проекта безопаснее синхронизировать весь исходный каталог, исключив
локальное окружение, git и секреты:

```bash
rsync -av --progress \
  -e 'ssh -o IdentitiesOnly=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no' \
  --exclude '.git/' --exclude 'venv/' --exclude '__pycache__/' \
  --exclude '.env' --exclude 'planiruy.env' \
  ./ root@185.199.197.107:/home/planiruy/
```

Файл `/home/planiruy/planiruy.env` на сервере команда не перезапишет.

## 3. Сборка и запуск

```bash
cd /home/planiruy

docker compose \
  -f docker-compose.yml \
  -f docker-compose.intervals.yml \
  -f docker-compose.max.yml \
  --env-file /home/planiruy/planiruy.env \
  up -d --build db app bot max_app
```

Конфигурацию nginx менять не требуется, если уже работают оба адреса:

- `https://planwithai.ru/` → `127.0.0.1:8000`;
- `https://planwithai.ru/max/` → `127.0.0.1:8001`.

## 4. Проверка

```bash
curl -fsS https://planwithai.ru/max/health
curl -fsS -o /dev/null -w '%{http_code}\n' https://planwithai.ru/login

docker compose \
  -f docker-compose.yml -f docker-compose.intervals.yml -f docker-compose.max.yml \
  --env-file /home/planiruy/planiruy.env \
  ps

docker compose \
  -f docker-compose.yml -f docker-compose.intervals.yml -f docker-compose.max.yml \
  --env-file /home/planiruy/planiruy.env \
  logs --tail=150 app bot max_app
```

Ожидается `{"status":"ok","service":"planiruy-max"}`, код `200`, а все четыре контейнера
должны быть в состоянии `Up`/`healthy` без циклических перезапусков.

## 5. Пользовательская проверка

1. Отправьте `/start` в обоих ботах: оба должны показать приветствие и кнопку календаря.
2. В Telegram отправьте `/link`, скопируйте код и отправьте в MAX: `/link КОД`.
3. Создайте событие в Telegram и убедитесь, что оно появилось в календаре MAX.
4. Потяните событие за середину, затем за верхнюю и нижнюю границу. После перезагрузки время
   должно сохраниться. Пересечение с другим событием должно быть отклонено.
5. Откройте `https://planwithai.ru/login` и проверьте оба входа. При входе через MAX сайт
   откроет бота с одноразовым кодом и автоматически продолжит после подтверждения.
6. Проверьте `/smart_reminders`, `/smart_reminders_off` и `/smart_reminders_on` в обоих ботах.
7. Напишите в любом боте `отмена НАЗВАНИЕ`: поиск должен видеть события обеих платформ.

Если приветствие MAX не приходит, перезапустите `max_app` и проверьте лог строки регистрации
webhook. Код автоматически обновляет старую подписку, добавляя событие `bot_started`.
