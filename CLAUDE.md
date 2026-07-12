# CueMe — контекст проекта

> Рабочее название `CueMe` временное (есть конфликт по бренду — менять позже).
> Имя продукта держим в `config.py` (`APP_NAME`), чтобы смена названия была правкой одной строки.
> Не вшивай название в имена пакетов, классов и таблиц БД.

## Позиционирование

CueMe — AI-ассистент для дейтинга и отношений в Telegram.

**Главная цель:** помочь пользователю лучше общаться с партнёром, нравиться ему/ей,
вызывать интерес и attraction, двигаться к близости и реальным встречам.

**Целевой пользователь:** активный дейтер 18-30 лет, ведёт переписки в Telegram,
хочет нравиться конкретному человеку и переводить общение в реальные встречи.

**Тон бота к пользователю:** уверенный дейтинг-коуч — прямой, без занудства,
говорит что реально работает. Коучинговый стиль в пояснениях и советах,
но сам переписанный текст — строго в голосе пользователя.

## Что делает — пять слоёв

1. **Модель себя** — анализ собственного стиля юзера (голос, тон, длина, регистр).
   Строится на его сообщениях из JSON-экспорта и business-потока.

2. **Профиль собеседника** — что ему/ей нравится, как реагирует, что вызывает интерес,
   что отталкивает, характерные паттерны общения.

3. **Ответ-ассистент** — переписывает черновик под стиль и предпочтения собеседника,
   сохраняя голос пользователя. Выбор стиля: флирт / юмор / нежно / уверенно /
   дружески / формально.

4. **Анализ собеседника** (кнопка «🔬 Анализ собеседника»), 4 блока:
   - Совместимость (0-100) с объяснением оценки
   - Как писать этому человеку — ритм, приёмы, реальные примеры удачных заходов из переписки
   - Длина, ритм, регистр и язык собеседника
   - 💚 Зелёные / 🚩 Красные флаги (честно, без выдумывания для симметрии)

5. **Скриншот → ответ** (кнопка «📸 По скриншоту») — Vision-модель читает скриншот
   переписки, пользователь выбирает стиль, получает готовый ответ.

## Два источника данных

1. **JSON-экспорт** (Telegram Desktop → result.json): пользователь загружает файл,
   парсер делит на my_messages / contact_messages, считаем признаки локально.
2. **Telegram Business API** (в приложении: Настройки → «Изменить» у профиля →
   Автоматизация чатов → «Ответы на сообщения»; путь проверен вручную на iPhone,
   Android предположительно зеркалит iOS; со стороны бота — Secretary Mode
   в BotFather): бот получает живой поток всех сообщений подключённых чатов
   в реальном времени, сохраняет в business_messages. Работает без Premium
   (с обновлением май 2026). Инструкция в боте (`_business_connect_text`) —
   с учётом платформы (iPhone/Android/десктоп отличаются шагами 1-2).

## Архитектура (минимизировать вызовы LLM)

Пайплайн:
1. Загрузка JSON → parse_chat() → extract_features() → save_message_samples() [без LLM]
2. При нажатии кнопки → ленивая генерация карточек:
   - build_style_card() → style_card (голос юзера)
   - build_interaction_card() → interaction_card (паттерны собеседника)
   - build_my_style_for_contact() → per-контактный стиль юзера
   - build_overall_style() → агрегат всех per-контактных стилей
3. Переписать → rewrite_message(draft, style_card, interaction_card, style)
4. Анализ собеседника → одним LLM-вызовом, кэш в deep_analysis
5. Business-поток → накопление в business_messages → триггер пересборки карточек
   по накоплению (REBUILD_THRESHOLD=50 сообщений)

LLM вызывается лениво — только при запросе пользователя, результаты кэшируются.

## Стек

- Бот: **aiogram 3.x** (Python 3.13, команда запуска: `py -3.13 main.py`)
- Парсинг: stdlib `json`
- Локальные признаки: `features.py` (чистый Python, без LLM)
- Парсер JSON-экспорта: `tg_parser.py`
- LLM: **Groq API**, модель `llama-3.3-70b-versatile` (основная),
  `llama-3.2-11b-vision-preview` (Vision для скриншотов)
- HTTP: **httpx** с `trust_env=False` (обход SOCKS-прокси)
- Хранилище: **SQLite** (bot.db)
- Платежи: Tribute (канал-пропуск, см. «Подписка» ниже) — НЕ Telegram Stars/YooKassa
- Хостинг: VPS
- Секреты: `.env` через `config.py`

## Схема БД (bot.db)

```
users(telegram_id PK, my_id, created_at, auto_mode, auto_contact_id,
      last_style_rebuild_count, trial_used, gender)
      -- gender: 'male' | 'female' | NULL, спрашивается в самом начале
      -- (GenderGateMiddleware в main.py блокирует всё взаимодействие, пока не
      -- выбран); нужен для согласования рода в промптах (llm.py: _gender_note)
      -- и обращения к пользователю. Меняется командой /gender.
contacts(id PK, user_telegram_id, contact_alias UUID,
         original_from_id, display_name)
style_cards(user_telegram_id PK, card_text, updated_at)
interaction_cards(contact_id PK, card_text, updated_at)
message_samples(contact_id PK, my_sample JSON, contact_sample JSON,
                features_summary, user_features_summary)
my_style_per_contact(contact_id PK, card_text, updated_at, last_rebuild_count)
business_connections(connection_id PK, owner_user_id, can_reply,
                     is_enabled, created_at)
business_messages(id PK AUTO, connection_id, owner_user_id,
                  chat_ref TEXT,     -- sha256(chat_id)[:16]
                  direction TEXT,    -- 'out' | 'in'
                  text, date, tg_message_id, raw_meta JSON)
business_chat_refs(owner_user_id, chat_ref, contact_id,
                   PRIMARY KEY (owner_user_id, chat_ref))
deep_analysis(contact_id PK, compatibility_text, history_text,
              swot_text, gifts_text, updated_at)
deep_style_analysis(user_telegram_id PK, profile_text, history_text,
                    swot_text, tips_text, updated_at)
events(id PK AUTO, ts, user_telegram_id, event_type, meta)  -- продуктовая
       -- аналитика: record_event / count_events / event_funnel (storage.py)
```

## Файлы проекта

- `main.py` — aiogram-хендлеры, FSM, вся логика бота
- `storage.py` — SQLite, все таблицы и функции работы с БД
- `llm.py` — вызовы Groq через httpx (trust_env=False)
- `features.py` — локальные признаки переписки без LLM
- `tg_parser.py` — парсер JSON-экспорта Telegram Desktop
- `config.py` — константы из .env, APP_NAME = "CueMe"
- `PROMPTS.md` — все промпты бота (документация)

## Главное меню бота

```
[📝 Переписать]      [👤 Мой стиль]
[🔍 Стиль собеседника] [🔄 Авто-режим]
[📸 По скриншоту]   [🔬 Глубокий анализ]
[🎯 Мой стиль с ним] [📋 Контакты]
```

## Business API

- `@dp.business_connection()` — upsert в business_connections
- `@dp.business_message()` — direction по sender_id, chat_ref = sha256(chat_id)[:16],
  запись в business_messages, триггер _maybe_rebuild через asyncio.create_task
- Матчинг контакта: `f"user{str(event.from_user.id)}"` == `original_from_id` из contacts
- Защита от параллельных пересборок: module-level set `_rebuilding: set[int]`
- allowed_updates включает: message, callback_query, business_connection,
  business_message, edited_business_message, deleted_business_messages
- Secretary Mode включён в BotFather (мини-апп)

## Живой профиль (пересборка по накоплению)

Константы в config.py: REBUILD_THRESHOLD=50, SAMPLE_SIZE=150
При накоплении >= THRESHOLD новых сообщений по контакту:
1. Пересобрать my_style_per_contact (out-сообщения к этому контакту)
2. Пересобрать interaction_card (in-сообщения от этого контакта)
3. Пересобрать общий style_card (агрегат всех per-contact карточек)
Свежее важнее: берём последние SAMPLE_SIZE сообщений по date DESC.
Fallback: если business_messages < 30 — дополнять из message_samples.

## Подписка (Tribute, канал-пропуск)

Монетизация — через приватный Telegram-канал «CueMe Premium», подключённый к
Tribute (10% комиссия, без вебхука на нашей стороне):
1. Tribute продаёт подписку на канал и сам добавляет/убирает участников по
   оплате/отмене — вся логика продления и биллинга на их стороне.
2. Бот проверяет доступ через `bot.get_chat_member(PREMIUM_CHANNEL_ID, user_id)`
   — состоит в канале = подписка активна. Бот должен быть добавлен в канал
   админом, иначе `get_chat_member` не отдаёт статус чужого участника.
3. Результат кэшируется на `PREMIUM_CACHE_TTL` сек (по умолчанию 300) в
   module-level `_premium_cache`, чтобы не дёргать Telegram API на каждое
   сообщение.

Помимо приватного канала-пропуска есть отдельный **открытый** канал с
новостями/обновлениями CueMe — чисто маркетинг, к гейтингу отношения не имеет.

Модель доступа — жёсткий пейволл с триалом по числу запросов:
- `FREE_TRIAL_REQUESTS` (по умолчанию 5) бесплатных генераций на **Переписать /
  Ответить за меня / По скриншоту** суммарно — считается один раз на новый
  черновик/входящее/скриншот (не на «Перегенерировать»/«Другой стиль» внутри
  того же захода). Счётчик — `users.trial_used`.
- Загрузка JSON-экспорта — всегда бесплатна (чистый локальный парсинг, без LLM).
- Все остальные функции (анализ собеседника, анализ своего стиля, стиль
  собеседника, /compare, /rebuild_all) — только по активной подписке, без триала.
- Гейты: `_consume_trial_or_paywall()` (триал) и `_require_premium()` (жёсткий),
  обе в main.py, обёрнуты вокруг `_is_premium()`.

## Соглашения по коду

- plain text везде в LLM-выводе (НЕ JSON) — во избежание ошибок парсинга
- httpx с trust_env=False — обязательно, иначе падает на SOCKS-прокси
- Секреты только из .env через config.py, никогда в коде
- APP_NAME — единственное место с названием продукта
- Миграции БД через _add_column_if_missing / CREATE TABLE IF NOT EXISTS
- Не коммитить .env, bot.db, __pycache__

## Репозиторий

https://github.com/kurganprevedenie-lgtm/CueMe