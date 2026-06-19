# CBU Currency Dashboard — `cbu-fx-pipeline`

**Язык:** [English](README.md) · **Русский**

> Сквозной конвейер данных, который ежедневно собирает курсы валют из открытого
> API Центрального банка Узбекистана (ЦБУ), загружает их в схему «звезда» в
> PostgreSQL, рассчитывает дневные изменения в SQL и публикует файл Parquet для
> Power BI — с автоматическим ежедневным обновлением через GitHub Actions.

[![CI](https://github.com/tuychiyevkm/cbu-fx-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/tuychiyevkm/cbu-fx-pipeline/actions/workflows/ci.yml)
[![Daily fetch](https://github.com/tuychiyevkm/cbu-fx-pipeline/actions/workflows/daily-fetch.yml/badge.svg)](https://github.com/tuychiyevkm/cbu-fx-pipeline/actions/workflows/daily-fetch.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)

**Живое демо:** _добавьте сюда ссылку Power BI «Опубликовать в Интернете»_
**Превью дашборда:** см. [`docs/screenshots/`](docs/screenshots/) (добавляется после первого запуска).

---

## Задача

ЦБУ публикует официальные курсы для ~74 валют через открытый JSON API, но только
как ежедневный снимок — нет удобного исторического хранилища, готового к анализу,
и нет автоматического обновления. Проект превращает этот сырой поток в чистое
хранилище с возможностью запросов и в самообновляющийся BI-дашборд: тренды,
волатильность и сравнение валют доступны в один клик.

---

## Архитектура

```
CBU JSON API
   │  (Python ETL: fetch_daily.py / backfill.py)
   ▼
Supabase PostgreSQL  ──►  схема «звезда»: dim_currency + fact_rates
   │                       представление: v_rates_with_change (оконная функция LAG)
   │  (шаг экспорта записывает вывод представления)
   ▼
data/rates.parquet  ──(коммитится в репозиторий, отдаётся через)──►  raw.githubusercontent.com
   ▼
Power BI (веб-коннектор)  ──►  плановое обновление работает, БЕЗ шлюза
```

### Почему разделение «хранилище + слой отдачи»

Это осознанный паттерн из практики, а не «обходной путь»:

- **Тяжёлый SQL живёт в PostgreSQL.** Схема «звезда», оконные функции и дневное
  процентное изменение рассчитываются в базе — там, где и должна выполняться
  аналитика над множествами.
- **Power BI питается из удобного слоя отдачи.** Прямое подключение Power BI к
  облачному Postgres требует локального шлюза данных и приносит проблемы с
  TLS/сертификатами при плановом обновлении. Вместо этого конвейер экспортирует
  аналитическое представление в один файл Parquet, коммитит его и отдаёт по
  обычному HTTPS с `raw.githubusercontent.com`. Power BI читает его веб-коннектором
  — **без шлюза и без проблем с сертификатами**, а плановое обновление просто
  работает.

---

## Технологии

| Слой         | Выбор                                                       |
|--------------|--------------------------------------------------------------|
| Язык         | Python 3.12                                                  |
| Библиотеки   | `requests`, `psycopg2-binary`, `python-dotenv`, `pyarrow`, `pandas` |
| Хранилище    | PostgreSQL на Supabase (бесплатный тариф)                   |
| Слой отдачи  | Файл Parquet, отдаётся по GitHub raw URL                    |
| BI           | Power BI (веб-коннектор → Parquet)                          |
| Автоматизация| GitHub Actions (основной) + локальный cron / Task Scheduler (альт.) |
| Качество     | `ruff` (линт + формат), `pytest`                            |

---

## Модель данных

Классическая **схема «звезда»**: одно измерение, одна таблица фактов и
представление-слой отдачи.

**`dim_currency`** — одна строка на валюту:
`currency_code` (PK), `iso_numeric`, `name_en`, `name_ru`, `name_uz`,
`name_uz_cyrillic`, `nominal`.

**`fact_rates`** — одна строка на (дата, валюта):
`id` (PK), `rate_date`, `currency_code` (FK), `rate` `NUMERIC(18,4)`,
`rate_per_unit` `NUMERIC(18,6)`, `diff` `NUMERIC(18,4)`,
`UNIQUE(rate_date, currency_code)`.

**`v_rates_with_change`** — соединяет факт и измерение и использует
`LAG(rate_per_unit) OVER (PARTITION BY currency_code ORDER BY rate_date)` для
расчёта `pct_change` — дневного процентного изменения стандартизованного курса.

### Две важные детали модели

- **`nominal` не всегда равен 1.** ЦБУ котирует IDR, IRR и VND **за 10 единиц**.
  Конвейер хранит `rate_per_unit = rate / nominal`, чтобы все валюты были
  напрямую сопоставимы; иначе страница сравнения ошибалась бы на порядок.
- **Процентное изменение считается один раз, в SQL — не в DAX.** Оно
  вычисляется в представлении на этапе ETL и экспортируется в Parquet. Расчёт
  один раз у источника (a) делает метрику одинаковой везде, где она
  используется, (b) избавляет от пересчёта при каждом взаимодействии со
  срезами в Power BI и (c) делает файл Parquet переносимым самодостаточным
  слоем отдачи.

---

## Установка

### 1. Создайте проект Supabase и получите строку подключения через пулер

1. Создайте бесплатный проект на [supabase.com](https://supabase.com).
2. **Project Settings → Database → Connection string → Session pooler.**
   Скопируйте её. Она использует **IPv4**-хост и форму имени пользователя
   `postgres.<project-ref>`:

   ```
   postgresql://postgres.<project-ref>:<password>@<region>.pooler.supabase.com:5432/postgres
   ```

   > Используйте **Session Pooler**, а не прямое подключение. Прямое
   > подключение работает только по IPv6, а раннеры GitHub Actions — только по
   > IPv4, поэтому прямая строка не работает в CI. Хост пулера — IPv4.

### 2. Настройте окружение и установите зависимости

```bash
cp .env.example .env          # затем вставьте ваш DATABASE_URL в .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Создайте схему и представление

Выполните SQL в редакторе Supabase (или через `psql`):

```bash
psql "$DATABASE_URL" -f sql/schema.sql
psql "$DATABASE_URL" -f sql/view_rates_with_change.sql
```

(`fetch_daily.py` и `backfill.py` также создают их автоматически.)

### 4. Загрузите историю за два года

```bash
python -m src.backfill            # последние 2 года, с возобновлением, с паузой
```

Бэкфилл пропускает нерабочие дни, пропускает уже загруженные даты и
переносит отдельные сбои запросов без аварийного завершения.

### 5. Сгенерируйте seed (для публичного репозитория)

```bash
python scripts/generate_seed.py   # берёт последние 30 дней из живого API
```

> **Если был приложен синтетический seed:** репозиторий может содержать явно
> помеченный синтетический seed (присутствует `data/seed/IS_SYNTHETIC.txt`),
> сгенерированный офлайн, чтобы проект можно было показать без сети. **Перед
> публикацией удалите `data/seed/IS_SYNTHETIC.txt` и заново выполните
> `python scripts/generate_seed.py --live`**, чтобы зафиксированный seed
> содержал реальные данные ЦБУ.

---

## Автоматизация

### GitHub Actions (основной способ)

Два воркфлоу:

- **`ci.yml`** — на каждый push/PR: `ruff check`, `ruff format --check`,
  `pytest` на Python 3.12.
- **`daily-fetch.yml`** — `cron: "0 7 * * *"` (= **12:00 Asia/Tashkent**, после
  публикации ЦБУ) плюс ручной запуск `workflow_dispatch`. Запускает
  `fetch_daily`, перегенерирует `data/rates.parquet` и коммитит его обратно,
  если он изменился.

**Необходимый секрет репозитория:**

| Имя секрета    | Значение                                                   |
|----------------|-------------------------------------------------------------|
| `DATABASE_URL` | Строка подключения Supabase **Session Pooler (IPv4)**.     |

Задайте его в **Settings → Secrets and variables → Actions → New repository
secret**. Ничего не зашивается в код.

### Локальный cron / Task Scheduler (документированная альтернатива)

**cron в Linux/macOS** (12:00 Ташкент = 07:00 UTC):

```cron
0 7 * * * cd /path/to/cbu-fx-pipeline && /path/to/.venv/bin/python -m src.fetch_daily >> fetch.log 2>&1
```

**Планировщик заданий Windows:** создайте ежедневную задачу на 12:00 локального
времени, запускающую `python -m src.fetch_daily` в каталоге проекта, с
переменной окружения `DATABASE_URL`.

---

## Power BI

Дашборд читает файл Parquet по HTTPS — шлюз не нужен.

1. **Получить данные → Из Интернета**, URL:
   `https://raw.githubusercontent.com/tuychiyevkm/cbu-fx-pipeline/main/data/rates.parquet`
   (анонимная аутентификация).
2. **Вид → Темы → Обзор** и примените [`powerbi/theme.json`](powerbi/theme.json).
3. Соберите три страницы по
   [`powerbi/BUILD_INSTRUCTIONS.md`](powerbi/BUILD_INSTRUCTIONS.md):
   - **Overview** — KPI-карточки (USD, EUR, RUB, GBP, CNY) с зелёным ростом /
     красным падением, линейный график USD, топ-5 движений за день.
   - **History** — срез по валюте (все ~74), срез по диапазону дат, линейный
     график, детальная таблица.
   - **Comparison** — несколько валют, нормированных к 100 на начало диапазона,
     и сортируемая таблица процентных изменений.
4. Сохраните скриншоты в `docs/screenshots/` (см. README в этой папке).
5. **Опубликуйте в Интернете** для ссылки на живое демо.

> Бинарный `.pbix` **не прилагается**: корректный файл нельзя создать
> программно, а битый заглушечный файл был бы хуже отсутствующего. Инструкция
> воспроизводит отчёт примерно за 15 минут. **Плановое обновление** в Power BI
> Service требует **Power BI Pro** (покрывается 60-дневным триалом); независимо
> от тарифа обновления, Parquet на GitHub обновляется ежедневно, поэтому ручное
> обновление всегда подтягивает свежие данные.

### Фирменная палитра

| Роль          | Hex       |
|---------------|-----------|
| Основной      | `#1B2A4A` |
| Акцент        | `#17A398` |
| Выделение     | `#F2A93B` |
| Рост (зелёный)| `#2ECC71` |
| Падение (красный)| `#E15554` |
| Фон           | `#F7F9FC` |
| Текст         | `#2D2D2D` |

---

## Витрина SQL

Дневное изменение через оконную функцию `LAG` (ядро представления-отдачи):

```sql
SELECT
    rate_date,
    currency_code,
    rate_per_unit,
    ROUND(
        (rate_per_unit - LAG(rate_per_unit) OVER w)
        / LAG(rate_per_unit) OVER w * 100, 4
    ) AS pct_change
FROM fact_rates
WINDOW w AS (PARTITION BY currency_code ORDER BY rate_date);
```

Рейтинг волатильности за 30 дней (стандартное отклонение дневного %, с ранжированием):

```sql
SELECT
    currency_code,
    ROUND(STDDEV_SAMP(pct_change), 4) AS volatility_30d
FROM v_rates_with_change
WHERE rate_date >= (SELECT MAX(rate_date) FROM fact_rates) - INTERVAL '30 days'
  AND pct_change IS NOT NULL
GROUP BY currency_code
ORDER BY volatility_30d DESC
LIMIT 10;
```

Больше — в [`sql/sample_queries.sql`](sql/sample_queries.sql) (скользящие
средние, нормирование к индексу, топ движений).

---

## Структура проекта

```
cbu-fx-pipeline/
├── src/
│   ├── cbu_client.py        # HTTP + парсинг (Decimal, DD.MM.YYYY, nominal)
│   ├── database.py          # psycopg2 (Session Pooler), upsert, экспорт в parquet
│   ├── fetch_daily.py       # точка входа: взять день, upsert, перегенерировать
│   └── backfill.py          # точка входа: бэкфилл за 2 года, с возобновлением
├── scripts/generate_seed.py # seed за 30 дней (live) + синтетический --demo
├── sql/                     # schema.sql, представление, sample_queries.sql
├── tests/                   # тесты парсера + идемпотентности upsert
├── powerbi/                 # theme.json + BUILD_INSTRUCTIONS.md
├── data/                    # rates.parquet (отдача) + seed/
├── docs/screenshots/        # скриншоты дашборда (после первого запуска)
├── .github/workflows/       # ci.yml + daily-fetch.yml
├── .env.example
├── requirements.txt
├── pyproject.toml
└── LICENSE
```

---

## Лицензия

[MIT](LICENSE) © 2026 Komron Toychiyev.

Данные о курсах валют © Центральный банк Республики Узбекистан,
через открытый API [cbu.uz](https://cbu.uz).
