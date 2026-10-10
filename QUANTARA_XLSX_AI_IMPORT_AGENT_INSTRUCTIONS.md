# Quantara — инструкция агенту по реализации AI-импорта XLSX

## 0. Цель

Реализовать в Quantara импорт материалов из `.xlsx` с помощью гибридного пайплайна:

```text
XLSX
→ детерминированное чтение таблицы
→ определение структуры/колонок
→ LLM только для семантического mapping неоднозначных колонок
→ детерминированная нормализация
→ детерминированная валидация
→ preview пользователю
→ ручная коррекция mapping/строк
→ подтверждение
→ bulk insert/update в PostgreSQL
```

Главный принцип: **LLM не должна напрямую писать данные в основные таблицы** (`reagents`, `consumables`, `equipment`) и не должна использоваться там, где задачу надёжно решает обычный код.

Нельзя менять существующий функционал ручного добавления, редактирования, удаления, списания и просмотра материалов.

## 1. Текущая архитектура

```text
Frontend:
React + Vite + TypeScript

Backend:
Fastify + TypeScript

DB access:
Drizzle ORM

Database:
PostgreSQL
```

Базовый поток:

```text
React
  ↓
/api/*
  ↓
Fastify
  ↓
Drizzle
  ↓
PostgreSQL
```

Новый импорт должен быть встроен в эту архитектуру. Не добавлять второй backend, отдельную БД, Supabase, микросервисы или новый frontend framework.

## 2. Пользовательский сценарий

Минимально поддержать импорт для:

```text
reagents
consumables
equipment
```

На соответствующих страницах добавить кнопку:

```text
+ Добавить
Импортировать XLSX
```

Существующая кнопка ручного добавления должна продолжать работать без изменений.

UX:

```text
1. Пользователь нажимает "Импортировать XLSX".
2. Выбирает .xlsx.
3. Backend разбирает workbook.
4. Система определяет:
   - лист;
   - строку заголовков;
   - тип сущности;
   - соответствие исходных колонок полям Quantara.
5. Открывается preview.
6. Пользователь видит mapping колонок.
7. Пользователь может изменить mapping.
8. Пользователь видит распарсенные строки.
9. Ошибочные/сомнительные строки помечены.
10. Пользователь может исправить значения.
11. Пользователь подтверждает импорт.
12. Backend одной транзакцией переносит подтверждённые данные
    в основную таблицу.
13. Пользователь видит итог:
    - добавлено;
    - обновлено;
    - пропущено;
    - ошибок.
```

Нельзя сразу после загрузки файла делать INSERT в основные таблицы.

## 3. Что НЕ должна делать LLM

LLM не должна:

- читать бинарный `.xlsx`;
- самостоятельно вычислять `stock`;
- конвертировать числа;
- выполнять SQL;
- делать INSERT/UPDATE;
- определять `org_id`;
- определять права пользователя;
- быть единственным механизмом валидации;
- выдумывать отсутствующие значения;
- исправлять CAS/даты/единицы без достаточных данных;
- получать весь файл на тысячи строк без необходимости.

LLM используется только для семантически неоднозначных задач:

1. определить значение нестандартных заголовков;
2. при необходимости помочь определить строку заголовка;
3. определить тип таблицы, если это неоднозначно;
4. в будущем — разобрать редкие текстовые значения, которые не удалось обработать детерминированно.

## 4. Backend: новая структура

Добавить модуль:

```text
backend/src/imports/
├── routes.ts
├── service.ts
├── excel-parser.ts
├── table-detector.ts
├── mapper.ts
├── llm-mapper.ts
├── normalizer.ts
├── validator.ts
├── duplicate-detector.ts
├── commit.ts
├── schemas.ts
└── types.ts
```

Если в текущем проекте принята другая структура модулей, адаптировать названия, но сохранить разделение ответственности.

## 5. XLSX parser

Предпочтительно использовать `exceljs`.

```ts
import ExcelJS from 'exceljs';

const workbook = new ExcelJS.Workbook();
await workbook.xlsx.load(buffer);
```

Не отправлять файл целиком во внешний LLM API.

## 6. Новые таблицы PostgreSQL

### 6.1. `import_jobs`

```text
import_jobs
-----------
id                  uuid primary key
org_id              текущий тип org_id
user_id             текущий тип user_id

entity_type         text
                    reagent
                    consumable
                    equipment

file_name           text
sheet_name          text nullable

status              text
                    uploaded
                    parsing
                    preview
                    ready
                    committing
                    completed
                    failed
                    cancelled

header_row          integer nullable
data_start_row      integer nullable

column_mapping      jsonb
parser_metadata     jsonb nullable

total_rows          integer default 0
ready_rows          integer default 0
warning_rows        integer default 0
error_rows          integer default 0

created_at          timestamptz
updated_at          timestamptz
committed_at        timestamptz nullable
```

`org_id` и `user_id` всегда определяются backend по текущей сессии.

### 6.2. `import_rows`

```text
import_rows
-----------
id                  uuid primary key
import_id           uuid references import_jobs(id)

row_number          integer

raw_data            jsonb
parsed_data         jsonb

status              text
                    ready
                    warning
                    error
                    skipped
                    duplicate
                    resolved

errors              jsonb
warnings            jsonb

duplicate_target_id uuid nullable
duplicate_action    text nullable
                    create
                    update
                    skip

created_at          timestamptz
updated_at          timestamptz
```

Добавить индекс по `import_id`.

## 7. Drizzle schema + migrations

Нужно:

1. добавить таблицы в Drizzle schema;
2. создать migration;
3. не изменять основные таблицы без необходимости;
4. migration должна работать на чистой PostgreSQL.

Не создавать таблицы вручную вне migration.

## 8. Целевые поля

### Reagents

Использовать реальные поля текущей schema. Минимально:

```text
name
category
cas_number
formula
concentration
storage
stock
unit
min_stock
expiry_date
location
supplier
project_id
```

Не импортировать:

```text
id
org_id
created_at
updated_at
deleted_at
```

`name` обязателен.

### Consumables

```text
name
category
stock
unit
min_stock
location
supplier
comment
```

`name` обязателен.

### Equipment

Использовать реальные поля текущей таблицы. Минимально:

```text
name
inventory_number
serial_number
model
manufacturer
category
location
responsible_id
status
quantity
notes

purchase_date
purchase_price
commissioning_date
useful_life_months

is_measuring_instrument
verification_date
next_verification_date
verification_certificate

last_service_date
next_service_date
service_provider
warranty_until
```

Не создавать новые имена полей, если в проекте они называются иначе.

## 9. Парсинг workbook

Backend должен:

1. проверить MIME/type и расширение;
2. ограничить размер файла;
3. открыть workbook;
4. получить список листов;
5. определить лист с таблицей;
6. определить строку заголовка;
7. определить начало данных;
8. преобразовать значения Excel в JSON.

Нельзя предполагать, что заголовок всегда находится в строке 1.

## 10. Определение header row

Сначала использовать детерминированную эвристику:

- количество непустых ячеек;
- доля текстовых значений;
- похожая структура следующих строк;
- ключевые слова: `название`, `наименование`, `CAS`, `количество`, `остаток`, `ед`, `склад`, `инвентарный`, `производитель`.

Если confidence недостаточен, можно вызвать LLM только на нескольких строках вокруг кандидатов.

## 11. Определение типа сущности

Правила первого уровня:

```text
CAS / formula / concentration
→ reagent

inventory_number / serial_number / verification
→ equipment

stock + unit + generic item name
→ consumable
```

Если неоднозначно — LLM.

Frontend всегда должен позволять вручную изменить тип до commit.

## 12. Column mapping

Пример:

```text
Название вещества → name
CAS № → cas_number
Кол-во на складе → stock
Ед.изм. → unit
Где хранится → location
```

## 13. Сначала deterministic mapping

Сделать словарь синонимов.

Примеры:

```text
name:
название
наименование
реактив
материал
вещество
товар
позиция

stock:
остаток
количество
кол-во
qty
quantity
количество на складе
текущий остаток

unit:
ед
ед.
ед.изм.
единица
единица измерения
unit

cas_number:
cas
cas №
cas no
cas number

location:
место
место хранения
склад
где хранится
location
```

Нормализовать заголовки: trim, lowercase, `ё→е`, схлопывание пробелов, удаление лишней пунктуации.

Если mapping найден однозначно — LLM не вызывать.

## 14. LLM mapping

LLM получает только:

- `entity_type`;
- source headers;
- допустимые target fields;
- тип/обязательность target field;
- 10–20 sample rows;
- при необходимости несколько дополнительных representative rows.

LLM возвращает строгий JSON:

```json
{
  "entity_type": "reagent",
  "columns": [
    {
      "source": "Наименование реактива",
      "target": "name",
      "confidence": 0.99
    },
    {
      "source": "Ост.",
      "target": "stock",
      "confidence": 0.87
    },
    {
      "source": "Примечания",
      "target": null,
      "confidence": 0.96
    }
  ]
}
```

`target` может быть только из разрешённого enum или `null`.

Использовать structured output / JSON Schema, если API это поддерживает.

## 15. LLM provider abstraction

Не зашивать конкретную модель прямо в route.

```ts
interface ImportMappingProvider {
  inferMapping(input: MappingInput): Promise<MappingResult>;
}
```

ENV:

```text
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
```

Если LLM недоступна, импорт должен работать через deterministic mapping + ручной mapping.

## 16. Нормализация

### Числа

Поддержать:

```text
"10"
"10.5"
"10,5"
"1 000"
"1 000,25"
numeric Excel cell
```

Результат: `number | null`.

### Даты

Поддержать:

- Excel date;
- ISO;
- `DD.MM.YYYY`;
- ограниченный набор известных форматов.

Не угадывать неоднозначные даты.

### Пустые значения

Аккуратно обрабатывать:

```text
""
"-"
"—"
"N/A"
```

Не превращать любое текстовое `"нет"` в null для произвольного поля.

## 17. Единицы измерения

Нормализовать написание:

```text
литр / литров / L / л. → л
миллилитр / ml → мл
грамм / g → г
килограмм / kg → кг
штука / шт. → шт
```

Не делать автоматическую конвертацию количества между единицами, пока в продукте нет единой внутренней системы единиц.

## 18. CAS validation

Для reagent:

1. проверить формат;
2. по возможности checksum;
3. не исправлять CAS автоматически;
4. ошибку показывать пользователю.

## 19. Validation

Каждая строка получает:

```text
ready
warning
error
```

Проверки минимум:

```text
name существует
stock является числом
stock >= 0
дата корректна
equipment.status допустим
responsible_id существует
project_id существует
```

Ошибки хранить структурированно:

```json
[
  {
    "field": "stock",
    "code": "INVALID_NUMBER",
    "message": "Не удалось распознать количество"
  }
]
```

## 20. API

### Загрузка

```http
POST /api/imports/xlsx
Content-Type: multipart/form-data
```

Поля:

```text
file
entity_type? optional
```

### Получить import job

```http
GET /api/imports/:id
```

### Получить строки

```http
GET /api/imports/:id/rows?page=1&page_size=100
```

Pagination обязательна.

### Изменить mapping

```http
PATCH /api/imports/:id/mapping
```

После изменения mapping backend повторно парсит строки детерминированно. LLM повторно не вызывать без необходимости.

### Изменить строку

```http
PATCH /api/imports/:id/rows/:rowId
```

Backend повторно нормализует/валидирует строку.

### Commit

```http
POST /api/imports/:id/commit
```

Backend берёт данные из `import_rows`, а не доверяет присланному frontend массиву.

## 21. Commit transaction

Commit обязательно делать транзакционно.

```ts
await db.transaction(async (tx) => {
  const job = await lockImportJob(tx, importId);
  validateJobCanCommit(job);

  const rows = await getImportRows(tx, importId);

  // bulk insert/update

  await markJobCompleted(tx, importId);
  await writeActivityLog(tx, ...);
});
```

Защититься от двойного commit через row lock или атомарный переход статуса.

## 22. Bulk insert

Не делать N HTTP POST запросов.

Нужно:

```text
1 upload
1 preview flow
1 commit
```

Backend использует bulk insert / batch insert.

## 23. Activity log

После успешного импорта:

```text
event_type = import_reagents
module = reagents
```

metadata:

```json
{
  "import_id": "...",
  "file_name": "остатки.xlsx",
  "created": 428,
  "updated": 31,
  "skipped": 17,
  "errors": 0
}
```

Не создавать сотни лишних событий, если это ломает текущую Analytics.

## 24. Duplicate detection

### Reagents

Сильный сигнал:

```text
org_id + cas_number
```

Дополнительно:

```text
normalized name
location
supplier
```

### Equipment

Проверять:

```text
inventory_number
serial_number
```

### Consumables

Conservative matching:

```text
normalized name + unit + location
```

При неоднозначности не обновлять автоматически.

## 25. UX дублей

Показывать:

```text
Новая
Возможный дубль
Ошибка
```

Действия:

```text
Создать отдельно
Обновить существующий
Пропустить
```

Default для неоднозначного совпадения — не обновлять автоматически.

## 26. Frontend

Не переписывать существующие страницы целиком.

Добавить reusable flow:

```text
src/components/import/
├── ImportButton.tsx
├── ImportDialog.tsx
├── ImportUploadStep.tsx
├── ImportMappingStep.tsx
├── ImportPreviewStep.tsx
├── ImportRowEditor.tsx
├── ImportDuplicateResolver.tsx
└── ImportResultStep.tsx
```

## 27. Upload UI

Поддержать `.xlsx`, показать понятные ошибки размера/формата. Backend повторяет все проверки.

## 28. Mapping UI

Пример:

```text
Исходная колонка          Поле Quantara

Наименование реактива    [Название ▼]
CAS №                     [CAS ▼]
Ост.                      [Остаток ▼]
Ед.изм.                   [Единица ▼]
Комментарий               [Не импортировать ▼]
```

Пользователь может изменить любой mapping.

В первой версии запретить два source column → один target field, если нет специальной логики merge.

## 29. Preview UI

Показывать таблицу и статусы:

```text
✓ готово
! предупреждение
✕ ошибка
≈ возможный дубль
```

Фильтры:

```text
Все
Готовые
С предупреждениями
Ошибки
Дубли
```

## 30. Редактирование строки

Пользователь может исправлять значения без повторной загрузки файла.

После save:

```text
PATCH import row
→ backend normalize/validate
→ новый status
```

## 31. Commit UX

Кнопка:

```text
Импортировать 476 записей
```

Показывать summary:

```text
428 новых
31 обновление
17 пропущено
0 ошибок
```

Ошибочные строки не импортировать. Корректные строки можно импортировать отдельно.

## 32. Permissions

Использовать те же права, что у ручного CRUD соответствующей сущности.

Если пользователь может вручную создать reagent, он должен иметь право импортировать reagent.

Backend проверяет права самостоятельно.

## 33. Multi-tenant isolation

Все запросы к:

```text
import_jobs
import_rows
target tables
```

обязаны фильтроваться по текущему `org_id`.

Пользователь org A не должен видеть import org B.

## 34. Хранение файла

Для MVP предпочтительно:

```text
upload buffer
→ parsing
→ import_rows
→ удалить исходный buffer
```

Оригинальный XLSX не хранить постоянно без явной бизнес-потребности.

## 35. Ограничения

Config:

```text
IMPORT_MAX_FILE_MB
IMPORT_MAX_ROWS
IMPORT_MAX_SHEETS
```

Стартово:

```text
20 MB
20 000 rows
20 sheets
```

## 36. Безопасность XLSX

- не исполнять макросы;
- не выполнять ссылки;
- не доверять filename;
- ограничивать память;
- отклонять unsupported/password-protected files;
- `.xls` можно не поддерживать в MVP.

## 37. Ошибки API

Использовать codes:

```text
IMPORT_INVALID_XLSX
IMPORT_TOO_LARGE
IMPORT_TOO_MANY_ROWS
IMPORT_NO_TABLE_FOUND
IMPORT_AMBIGUOUS_HEADER
IMPORT_INVALID_MAPPING
IMPORT_NOT_FOUND
IMPORT_FORBIDDEN
IMPORT_ALREADY_COMMITTED
IMPORT_HAS_UNRESOLVED_ERRORS
LLM_MAPPING_FAILED
```

Не отправлять raw stack trace пользователю.

## 38. Отказ LLM

Если LLM недоступна:

```text
deterministic mapping достаточен
→ продолжить

недостаточен
→ показать ручной mapping
```

Импорт не должен полностью зависеть от LLM.

## 39. Import templates — не обязательны для MVP

Архитектура должна позволять позже добавить сохранённые шаблоны mapping по сигнатуре заголовков.

## 40. Не ломать текущие CRUD endpoints

Текущие:

```text
/api/reagents
/api/consumables
/api/equipment
```

оставить.

Импорт реализовать отдельно:

```text
/api/imports/*
```

После commit записи должны быть обычными сущностями Quantara.

## 41. Общая validation

Не создавать две независимые схемы валидации.

Manual create и import должны использовать общий базовый validator/schema.

## 42. Backend tests

Обязательные unit tests:

### excel-parser
- обычный XLSX;
- header в первой строке;
- header после служебных строк;
- пустые строки;
- несколько листов;
- числа;
- даты.

### mapper
- exact mapping;
- synonym mapping;
- unknown column;
- conflict;
- LLM fallback.

### normalizer
- `10`;
- `10,5`;
- `1 000,25`;
- даты;
- единицы;
- null-like values.

### validator
- missing name;
- negative stock;
- invalid date;
- invalid equipment status;
- valid row.

### duplicate detector
- CAS match;
- inventory number match;
- ambiguous consumable.

## 43. Integration tests

Обязательные сценарии:

```text
upload → preview → mapping correction → commit → rows appear
```

```text
invalid row → fix → ready → commit
```

```text
duplicate → update → no second row
```

```text
org A import → org B access → 403/404
```

```text
commit twice → second commit does not duplicate rows
```

## 44. Frontend regression

Проверить:

- старое ручное добавление работает;
- edit/delete работают;
- import есть на Reagents;
- import есть на Consumables;
- import есть на Equipment при включённом module;
- upload/mapping/preview/edit/commit работают;
- импортированные записи редактируются обычным UI.

## 45. Производительность

Не отправлять весь workbook в React.

Preview rows получать с pagination.

Backend обрабатывает rows пачками.

## 46. Логи

Логировать:

```text
import_id
org_id
user_id
file_name
row_count
entity_type
duration
LLM used yes/no
created/updated/skipped/failed
```

Не логировать API keys и весь файл.

## 47. Существующие данные

Не менять существующие записи. Нужна только migration для `import_jobs`/`import_rows`.

## 48. Definition of Done

Готово, если:

1. frontend build проходит;
2. backend build проходит;
3. migrations проходят на чистой PostgreSQL;
4. старый CRUD не сломан;
5. `.xlsx` загружается;
6. header/mapping определяются;
7. LLM используется только как semantic fallback;
8. mapping можно исправить;
9. preview показывает ошибки/дубли;
10. строки можно исправить;
11. commit транзакционный;
12. повторный commit не создаёт дубли;
13. данные появляются в существующих таблицах;
14. tenant isolation работает;
15. без LLM остаётся ручной fallback.

## 49. Порядок реализации

```text
1. Изучить текущие Drizzle schemas и CRUD routes.
2. Зафиксировать реальные поля target tables.
3. Добавить import_jobs/import_rows.
4. Создать migration.
5. XLSX parser.
6. Header/table detector.
7. Deterministic mapper.
8. LLM mapper abstraction.
9. Normalizer.
10. Validator.
11. Preview endpoints.
12. Mapping/row correction.
13. Duplicate detector.
14. Transactional commit.
15. Frontend import dialog.
16. Mapping UI.
17. Preview/editor.
18. Duplicate resolution.
19. Result step.
20. Tests.
21. Regression текущего CRUD.
```

Не начинать с UI до проверки реальной схемы БД и backend contract.

## 50. Что агент должен предоставить в финальном отчёте

Перечислить:

```text
1. Добавленные файлы.
2. Изменённые файлы.
3. Migrations.
4. Новые endpoints.
5. LLM provider.
6. ENV variables.
7. Deterministic fallback.
8. Duplicate detection.
9. Запущенные тесты.
10. Frontend build result.
11. Backend build result.
12. Инструкция ручной проверки.
13. Известные ограничения.
```

Если что-то не реализовано/не протестировано — написать прямо.

## 51. Критическое архитектурное правило

```text
                    XLSX
                      ↓
          deterministic XLSX parser
                      ↓
           header / table detection
                      ↓
        ┌─────────────┴─────────────┐
        ↓                           ↓
deterministic mapping      LLM semantic fallback
        └─────────────┬─────────────┘
                      ↓
                 normalization
                      ↓
                  validation
                      ↓
              import_jobs/rows
                      ↓
                    PREVIEW
                      ↓
               user confirmation
                      ↓
             transaction commit
                      ↓
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
     reagents     consumables    equipment
```

**LLM никогда не должна иметь прямой путь к INSERT/UPDATE в основные таблицы.**
