"""Сервис карточек стиля: выборка сообщений, ленивая генерация карточек
(style_card / interaction_card / my_style_per_contact) и авто-пересборка по
накоплению (REBUILD_THRESHOLD).

Не содержит хендлеров — только логика, которую дёргают handlers/*.
Код перенесён из main.py без изменений (структурный рефакторинг).
"""
import logging
import time

from aiogram import Bot

from config import (
    FIRST_BUILD_THRESHOLD,
    REBUILD_THRESHOLD,
    REFRESH_SAMPLES_EVERY_N,
    SAMPLE_SIZE,
)
from handlers.common import _contact_name
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
    get_imported_messages,
    get_interaction_card,
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


# Защита от параллельных пересборок одного контакта
_rebuilding: set[int] = set()


# После лимита Groq фоновые авто-пересборки молчат до этого момента (monotonic-время),
# чтобы не дёргать API обречёнными запросами на каждое сообщение.
_rebuild_cooldown_until: float = 0.0


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
    my_msgs      = _get_rebuild_sample(owner_user_id, contact_id, "out", SAMPLE_SIZE)
    contact_msgs = _get_rebuild_sample(owner_user_id, contact_id, "in", SAMPLE_SIZE // 2)

    if not my_msgs:
        return False

    total   = count_biz_messages_for_contact(owner_user_id, contact_id)
    out_avg = sum(len(t) for t in my_msgs) / len(my_msgs)
    in_avg  = sum(len(t) for t in contact_msgs) / len(contact_msgs) if contact_msgs else 0
    stats = (
        f"Я: {total} сообщений всего, {len(my_msgs)} в выборке, средн. {out_avg:.0f} симв.\n"
        f"Собеседник: {len(contact_msgs)} в выборке, средн. {in_avg:.0f} симв."
    )

    # Обновляем message_samples объединёнными данными (JSON + business)
    contact_row = get_contact_by_id(contact_id)
    label = (contact_row["display_name"] or contact_row["contact_alias"]) if contact_row else ""
    save_message_samples(
        contact_id,
        my_msgs[:100],
        contact_msgs[:50],
        stats,
        contact_label=label,
    )

    my_style = await build_my_style_for_contact(my_msgs, stats)
    save_my_style_per_contact(contact_id, my_style, total)

    if contact_msgs:
        interaction = await build_interaction_card(my_msgs, contact_msgs, stats)
        save_interaction_card(contact_id, interaction)

    return True


# ── Авто-пересборка (fire-and-forget) ─────────────────────────────────────────

async def _maybe_rebuild(owner_user_id: str, contact_id: int, bot: Bot | None = None) -> None:
    global _rebuild_cooldown_until
    if contact_id in _rebuilding:
        return
    if time.monotonic() < _rebuild_cooldown_until:
        return  # лимит Groq недавно исчерпан — не дёргаем API на каждое сообщение

    last  = get_my_style_last_rebuild_count(contact_id)
    total = count_biz_messages_for_contact(owner_user_id, contact_id)

    # Первая сборка (карточки ещё нет) — сниженный порог, чтобы новый юзер
    # быстрее увидел результат; сообщения считаем из всех источников
    # (business + ручная вставка/JSON). Пересборка — обычный порог по biz-дельте.
    is_first = get_my_style_per_contact(contact_id) is None
    if is_first:
        combined = total + count_imported_messages(contact_id)
        if combined < FIRST_BUILD_THRESHOLD:
            return
    elif total - last < REBUILD_THRESHOLD:
        return

    # Порог достигнут — гарантируем свежие message_samples на момент пересборки
    # (перекрывает троттлинг на горячем пути) и сбрасываем счётчик.
    _refresh_pending.pop(contact_id, None)
    _refresh_samples(owner_user_id, contact_id)

    _rebuilding.add(contact_id)
    try:
        logging.info("auto-rebuild start: contact_id=%s (new=%s, first=%s)", contact_id, total - last, is_first)
        ok = await _rebuild_contact(owner_user_id, contact_id)
        if ok:
            per_contact = get_all_per_contact_style_cards(owner_user_id)
            if per_contact:
                overall = await build_overall_style(per_contact)
                save_style_card(owner_user_id, overall)
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
        _rebuild_cooldown_until = time.monotonic() + 1800
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
