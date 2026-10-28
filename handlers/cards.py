"""FSM и ленивая генерация / пересборка карточек.

Вынесено из main.py без изменения логики (строки 3564–3820 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import asyncio
import logging
import time
from aiogram import Bot
from aiogram.fsm.state import State, StatesGroup
from config import (
    FIRST_BUILD_THRESHOLD,
    REBUILD_THRESHOLD,
    REFRESH_SAMPLES_EVERY_N,
    SAMPLE_SIZE,
)
from llm import (
    RateLimitError,
    build_interaction_card,
    build_my_style_for_contact,
    build_overall_style,
    build_style_card,
)
from storage import (
    count_biz_messages_for_contact,
    count_imported_messages,
    get_all_per_contact_style_cards,
    get_any_user_samples,
    get_biz_messages_for_contact,
    get_contact_by_id,
    get_interaction_card,
    get_imported_messages,
    get_message_samples,
    get_my_style_last_rebuild_count,
    get_my_style_per_contact,
    get_running_notes,
    get_style_card,
    save_interaction_card,
    save_message_samples,
    save_my_style_per_contact,
    save_style_card,
)
from app_state import (
    _REBUILD_CONCURRENCY,
    _REBUILD_MAX_QUEUED,
    _contact_name,
    _rebuild_checking,
    _rebuild_slots,
    _rebuilding,
)
import app_state  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


# ── FSM ───────────────────────────────────────────────────────────────────────

class Setup(StatesGroup):
    waiting_for_json    = State()
    waiting_for_contact = State()

class ReplyHelp(StatesGroup):
    waiting_for_incoming = State()

# Screenshot (FSM для отдельной команды /screenshot) убрана вместе со всей
# функцией «скриншот переписки → ответ» — см. пометку у секции «Ответить по
# скриншоту» ниже. Класс оставлен закомментированным — некоторые функции по
# соседству (_run_variants_generation) раньше ссылались на него.
# class Screenshot(StatesGroup):
#     waiting_for_image = State()

class LiveDialogue(StatesGroup):
    waiting_for_name     = State()
    waiting_for_incoming = State()

class UnifiedReply(StatesGroup):
    """«💬 Ответ с CueMe» — единая точка входа вместо БТН_REPLY/BTN_LIVE
    (BTN_SCREENSHOT была третьей — фото-вход в эту же точку убран вместе с
    функцией «скриншот переписки → ответ», см. handle_unified_input): текст/
    форвард → определение контакта → приводит к одному из существующих
    пайплайнов (ReplyHelp для существующего контакта, LiveDialogue для
    нового)."""
    waiting_for_input = State()
    waiting_for_name  = State()


# ── Выборка: biz-сообщения + fallback из JSON-семплов ─────────────────────────

def _get_rebuild_sample(
    owner_user_id: str, contact_id: int, direction: str, limit: int
) -> list[str]:
    msgs = get_biz_messages_for_contact(owner_user_id, contact_id, direction, limit)
    seen = set(msgs)
    for t in get_imported_messages(contact_id, direction):  # всё из JSON без лимита
        if t not in seen:
            msgs.append(t)
    return msgs


def _quick_stats(my_msgs: list[str], contact_msgs: list[str]) -> str:
    """Лёгкая статистика без LLM — для поля features_summary."""
    out_avg = sum(len(t) for t in my_msgs) / len(my_msgs) if my_msgs else 0
    in_avg  = sum(len(t) for t in contact_msgs) / len(contact_msgs) if contact_msgs else 0
    return (
        f"Я: {len(my_msgs)} сообщ., средн. длина {out_avg:.0f} симв.\n"
        f"Собеседник: {len(contact_msgs)} сообщ., средн. длина {in_avg:.0f} симв."
    )


# Троттлинг _refresh_samples: считаем business-сообщения на контакт и обновляем
# message_samples не чаще, чем раз в REFRESH_SAMPLES_EVERY_N. In-memory — при рестарте
# сбрасывается, тогда первый refresh просто случится раньше (не критично).
_refresh_pending: dict[int, int] = {}


def _should_refresh_samples(contact_id: int) -> bool:
    """True раз в REFRESH_SAMPLES_EVERY_N сообщений на контакт (и сбрасывает счётчик)."""
    n = _refresh_pending.get(contact_id, 0) + 1
    if n >= REFRESH_SAMPLES_EVERY_N:
        _refresh_pending[contact_id] = 0
        return True
    _refresh_pending[contact_id] = n
    return False


def _refresh_samples(owner_user_id: str, contact_id: int) -> None:
    """Освежает message_samples из текущих business + imported данных. Без LLM."""
    my_full = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
    ct_full = _get_rebuild_sample(owner_user_id, contact_id, "in", SAMPLE_SIZE)
    c     = get_contact_by_id(contact_id)
    label = _contact_name(c) if c else ""
    save_message_samples(
        contact_id, my_full[:100], ct_full[:50], _quick_stats(my_full, ct_full), contact_label=label
    )


# ── Ядро пересборки одного контакта ──────────────────────────────────────────

async def _rebuild_contact(owner_user_id: str, contact_id: int) -> bool:
    """Пересобирает my_style_per_contact и interaction_card. True если успешно."""
    my_msgs      = await asyncio.to_thread(_get_rebuild_sample, owner_user_id, contact_id, "out", SAMPLE_SIZE)
    contact_msgs = await asyncio.to_thread(_get_rebuild_sample, owner_user_id, contact_id, "in", SAMPLE_SIZE // 2)

    if not my_msgs:
        return False

    total   = await asyncio.to_thread(count_biz_messages_for_contact, owner_user_id, contact_id)
    out_avg = sum(len(t) for t in my_msgs) / len(my_msgs)
    in_avg  = sum(len(t) for t in contact_msgs) / len(contact_msgs) if contact_msgs else 0
    stats = (
        f"Я: {total} сообщений всего, {len(my_msgs)} в выборке, средн. {out_avg:.0f} симв.\n"
        f"Собеседник: {len(contact_msgs)} в выборке, средн. {in_avg:.0f} симв."
    )

    # Обновляем message_samples объединёнными данными (JSON + business)
    contact_row = await asyncio.to_thread(get_contact_by_id, contact_id)
    label = (contact_row["display_name"] or contact_row["contact_alias"]) if contact_row else ""
    await asyncio.to_thread(
        save_message_samples, contact_id, my_msgs[:100], contact_msgs[:50], stats, contact_label=label,
    )

    my_style = await build_my_style_for_contact(my_msgs, stats)
    await asyncio.to_thread(save_my_style_per_contact, contact_id, my_style, total)

    if contact_msgs:
        interaction = await build_interaction_card(my_msgs, contact_msgs, stats)
        await asyncio.to_thread(save_interaction_card, contact_id, interaction)

    return True


# ── Авто-пересборка (fire-and-forget) ─────────────────────────────────────────

async def _maybe_rebuild(owner_user_id: str, contact_id: int, bot: Bot | None = None) -> None:
    # (было `global …`: имя живёт в другом модуле — обращение по ссылке ниже)
    if contact_id in _rebuilding or contact_id in _rebuild_checking:
        return
    if len(_rebuilding) + len(_rebuild_checking) >= _REBUILD_CONCURRENCY + _REBUILD_MAX_QUEUED:
        return  # очередь пересборок полна — запустится на следующем сообщении
    if time.monotonic() < app_state._rebuild_cooldown_until:
        return  # лимит Groq недавно исчерпан — не дёргаем API на каждое сообщение

    # Место в очереди занимаем сразу, ещё до подсчётов (они — в потоке, с
    # await): иначе лавина из десятков переписок проходит проверку очереди
    # раньше, чем хоть одна успевает в неё записаться.
    _rebuild_checking.add(contact_id)
    try:
        last  = await asyncio.to_thread(get_my_style_last_rebuild_count, contact_id)
        total = await asyncio.to_thread(count_biz_messages_for_contact, owner_user_id, contact_id)

        # Первая сборка (карточки ещё нет) — сниженный порог, чтобы новый юзер
        # быстрее увидел результат; сообщения считаем из всех источников
        # (business + ручная вставка/JSON). Пересборка — обычный порог по biz-дельте.
        is_first = await asyncio.to_thread(get_my_style_per_contact, contact_id) is None
        if is_first:
            combined = total + await asyncio.to_thread(count_imported_messages, contact_id)
            if combined < FIRST_BUILD_THRESHOLD:
                return
        elif total - last < REBUILD_THRESHOLD:
            return
        # Порог достигнут — дальше пересборка (свежие message_samples на момент
        # пересборки перекрывают троттлинг на горячем пути).
        _rebuilding.add(contact_id)
    finally:
        _rebuild_checking.discard(contact_id)

    try:
        async with _rebuild_slots:
            _refresh_pending.pop(contact_id, None)
            await asyncio.to_thread(_refresh_samples, owner_user_id, contact_id)
            logging.info("auto-rebuild start: contact_id=%s (new=%s, first=%s)", contact_id, total - last, is_first)
            ok = await _rebuild_contact(owner_user_id, contact_id)
            if ok:
                per_contact = await asyncio.to_thread(get_all_per_contact_style_cards, owner_user_id)
                if per_contact:
                    overall = await build_overall_style(per_contact)
                    await asyncio.to_thread(save_style_card, owner_user_id, overall)
            logging.info("auto-rebuild done: contact_id=%s ok=%s", contact_id, ok)

        if ok and is_first and bot is not None:
            # Первый разбор готов — короткое уведомление. Сама карточка сохранена
            # и доступна по кнопке «🔬 Анализ собеседника» (полотно не шлём).
            c = get_contact_by_id(contact_id)
            label = _contact_name(c) if c else "собеседником"
            try:
                await bot.send_message(
                    int(owner_user_id),
                    f"✅ Собралось достаточно сообщений с {label} — готов «🔬 Анализ "
                    "собеседника». Открой его в меню, чтобы посмотреть разбор.",
                )
            except Exception:
                logging.warning("first-build notify failed: owner=%s", owner_user_id)
    except RateLimitError:
        # Дневной лимит исчерпан — молчим 30 мин, пересоберём позже. Без трейсбека.
        app_state._rebuild_cooldown_until = time.monotonic() + 1800
        logging.warning("auto-rebuild отложена на 30 мин (лимит Groq): contact_id=%s", contact_id)
    except Exception:
        logging.exception("auto-rebuild failed: contact_id=%s", contact_id)
    finally:
        _rebuilding.discard(contact_id)


# ── Ленивая генерация карточек ────────────────────────────────────────────────

async def _gen_style_card(telegram_id: str) -> str | None:
    """Общий агрегатный портрет. Приоритет: per-contact cards > JSON-семплы."""
    card = get_style_card(telegram_id)
    if card:
        return card

    per_contact = get_all_per_contact_style_cards(telegram_id)
    if per_contact:
        card = await build_overall_style(per_contact)
        save_style_card(telegram_id, card)
        return card

    # Fallback: старый подход через JSON-семплы
    samples = get_any_user_samples(telegram_id)
    if not samples:
        return None
    card = await build_style_card(samples["my_sample"], samples["features_summary"])
    save_style_card(telegram_id, card)
    return card


async def _gen_interaction_card(contact_id: int, owner_user_id: str = "") -> str | None:
    card = get_interaction_card(contact_id)
    if card:
        return card

    # «Живой диалог» уже накопил заметки о собеседнике — используем их напрямую,
    # пока не появится формально пересобранная карточка (без ожидания порога).
    notes = get_running_notes(contact_id)
    if notes and notes["notes_text"]:
        return notes["notes_text"]

    samples = get_message_samples(contact_id)
    if samples:
        my_msgs      = samples["my_sample"]
        contact_msgs = samples["contact_sample"]
        stats        = samples["features_summary"]
    elif owner_user_id:
        my_msgs      = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
        contact_msgs = _get_rebuild_sample(owner_user_id, contact_id, "in", SAMPLE_SIZE // 2)
        if not contact_msgs:
            return None
        out_avg = sum(len(t) for t in my_msgs) / len(my_msgs) if my_msgs else 0
        in_avg  = sum(len(t) for t in contact_msgs) / len(contact_msgs)
        stats   = f"Мои: {len(my_msgs)} сообщ., средн. {out_avg:.0f} симв. | Собеседника: {len(contact_msgs)} сообщ., средн. {in_avg:.0f} симв."
    else:
        return None

    card = await build_interaction_card(my_msgs, contact_msgs, stats)
    save_interaction_card(contact_id, card)
    return card


async def _gen_my_style_per_contact(contact_id: int, owner_user_id: str) -> str | None:
    card = get_my_style_per_contact(contact_id)
    if card:
        return card
    my_msgs = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
    if not my_msgs:
        return None
    total   = count_biz_messages_for_contact(owner_user_id, contact_id)
    out_avg = sum(len(t) for t in my_msgs) / len(my_msgs)
    stats   = f"Я: {total} сообщений, {len(my_msgs)} в выборке, средн. {out_avg:.0f} симв."
    card    = await build_my_style_for_contact(my_msgs, stats)
    save_my_style_per_contact(contact_id, card, total)
    return card
