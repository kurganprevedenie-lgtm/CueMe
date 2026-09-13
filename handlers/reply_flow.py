"""💬 Ответ с CueMe — единая точка входа в генерацию ответов.

Включает три связанных пайплайна: ответ существующему контакту (ReplyHelp),
живой диалог с нуля (LiveDialogue) и готовые фразы-открывашки без LLM.
Здесь же — «Другие варианты» и общий рендер вариантов.

Код перенесён из main.py без изменений (структурный рефакторинг), кроме
регистрации на собственном Router вместо глобального Dispatcher.
"""
import html
import json
import logging
import random
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    LLM_CACHE_TTL_SEC,
    OPENERS_FOR_HER,
    OPENERS_FOR_HIM,
    REVIVE_QUESTIONS,
)
from features import (
    detect_reply_situation,
    stage_hint,
    totals_from_summary,
    winning_messages,
)
from handlers.common import (
    LiveDialogue,
    ReplyHelp,
    UnifiedReply,
    _answer_long,
    _charge_trial_if_needed,
    _contact_name,
    _contact_words,
    _edit_or_answer_long,
    _get_action,
    _message_text,
    _new_action,
    _not_command,
    _quota_gate,
    _send_no_contacts_hint,
    _style_cache_key,
    _with_back_to_menu,
    contacts_kb,
)
from llm import (
    RateLimitError,
    analyze_reply_dynamics,
    live_coach_step,
    suggest_reply_variants,
)
from services.cards import _gen_interaction_card, _gen_my_style_per_contact, _gen_style_card
from storage import (
    get_all_dated_messages,
    get_contact_by_id,
    get_gender,
    get_interaction_card,
    get_llm_cache,
    get_message_samples,
    get_my_style_per_contact,
    get_or_create_contact,
    get_running_notes,
    list_contacts,
    record_event,
    save_running_notes,
    save_suggestions,
    set_llm_cache,
    upsert_user,
)

router = Router(name="reply_flow")


# ── Хелпер: стиль для перезаписи (per-contact → global fallback) ──────────────

async def _style_for_rewrite(telegram_id: str, contact_id: int) -> str | None:
    """Предпочитаем per-contact карточку чтобы не смешивать данные разных чатов."""
    card = get_my_style_per_contact(contact_id)
    if card:
        return card
    return await _gen_style_card(telegram_id)


# Кнопка «📝 Переписать» и /rewrite убраны совсем — сценарий (черновик без
# привязки к входящему) теперь закрывает «💫 Новый диалог». _style_for_rewrite
# выше не удалена — общий хелпер, используется reply/screenshot тоже.


# ── 💬 Ответить за меня ───────────────────────────────────────────────────────

async def _start_reply(message: Message, state: FSMContext) -> None:
    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await _send_no_contacts_hint(message)
        return

    if len(contacts) == 1:
        c = contacts[0]
        style_card       = await _style_for_rewrite(telegram_id, c["id"])
        interaction_card = get_interaction_card(c["id"])
        if not style_card or not interaction_card:
            await message.answer("Генерирую анализ — займёт ~20 секунд...")
            try:
                if not interaction_card:
                    interaction_card = await _gen_interaction_card(c["id"], telegram_id)
                if not style_card:
                    style_card = await _gen_style_card(telegram_id)
            except RateLimitError:
                await message.answer("Лимит LLM исчерпан, попробуй позже.")
                return
            except Exception:
                logging.exception("_start_reply: ошибка генерации карточек")
                await message.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
                return
        # Данных ещё нет (например контакт только что автосоздался от одного
        # исходящего сообщения, входящих ещё не было) — не тупик, отвечаем
        # нейтрально по смыслу, как и в холодном старте «Живого диалога».
        style_card = style_card or _LIVE_NEUTRAL_STYLE_PLACEHOLDER
        interaction_card = interaction_card or _NEUTRAL_INTERACTION_PLACEHOLDER
        await state.update_data(
            style_card=style_card, interaction_card=interaction_card, contact_id=c["id"]
        )
        await state.set_state(ReplyHelp.waiting_for_incoming)
        name = _contact_name(c)
        await message.answer(
            f"Перешли или вставь сообщение от {name}, на которое нужно ответить:"
        )
        return

    await message.answer("Кому отвечаешь?", reply_markup=contacts_kb(contacts, "reply"))


async def _ensure_reply_cards(
    target: Message, telegram_id: str, contact_id: int, edit: bool,
) -> tuple[str, str] | None:
    """style_card/interaction_card для «Ответить за меня» — генерирует
    недостающие. None при ошибке (сообщение об ошибке уже отправлено
    вызывающему). Нет данных не значит тупик — фолбэк на нейтральные
    плейсхолдеры, как в холодном старте «Живого диалога»."""
    style_card       = await _style_for_rewrite(telegram_id, contact_id)
    interaction_card = get_interaction_card(contact_id)
    if not style_card or not interaction_card:
        msg_fn = target.edit_text if edit else target.answer
        await msg_fn("Генерирую анализ — займёт ~20 секунд...")
        try:
            if not interaction_card:
                interaction_card = await _gen_interaction_card(contact_id, telegram_id)
            if not style_card:
                style_card = await _gen_style_card(telegram_id)
        except RateLimitError:
            await msg_fn("Лимит LLM исчерпан, попробуй позже.")
            return None
        except Exception:
            logging.exception("_ensure_reply_cards: ошибка генерации карточек")
            await msg_fn("Не удалось сгенерировать анализ — попробуй ещё раз.")
            return None

    return (
        style_card or _LIVE_NEUTRAL_STYLE_PLACEHOLDER,
        interaction_card or _NEUTRAL_INTERACTION_PLACEHOLDER,
    )


@router.callback_query(F.data.startswith("reply:"))
async def cb_reply_contact(call: CallbackQuery, state: FSMContext) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)

    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    await call.answer()

    cards = await _ensure_reply_cards(call.message, telegram_id, contact_id, edit=True)
    if cards is None:
        return
    style_card, interaction_card = cards

    await state.update_data(
        style_card=style_card, interaction_card=interaction_card, contact_id=contact_id
    )
    await state.set_state(ReplyHelp.waiting_for_incoming)
    name = _contact_name(contact)
    await call.message.edit_text(
        f"Перешли или вставь сообщение от {name}, на которое нужно ответить:"
    )


# ── 💬 Ответ с CueMe (единая точка входа вместо Скриншот/Ответить/Новый диалог) ──
# Текст/форвард → определение контакта → существующий пайплайн: ReplyHelp
# для выбранного контакта, LiveDialogue для нового. Фото (скриншот
# переписки) как вход убран вместе с функцией «скриншот → ответ» — см.
# пометку у handle_unified_input и у секции «Ответить по скриншоту» ниже.
#
# Вся фаза настройки (запрос текста → выбор/имя контакта → статус
# генерации) — ОДНО редактируемое сообщение, а не серия новых: message_id
# первого сообщения сохраняется в FSM (setup_chat_id/setup_message_id) и
# переиспользуется через bot.edit_message_text в handle_unified_input/
# handle_unified_name (это обычные message-хендлеры, не callback — своего
# "call.message" для edit у них нет). cb_unified_contact — callback на этом
# же сообщении, там подходит обычный call.message.edit_text. Итоговое
# сообщение с вариантами ответа (после настройки) — НОВОЕ, самостоятельное,
# см. _run_variants_generation/_run_live_coach_step.

async def _start_unified_reply(message: Message, state: FSMContext) -> None:
    await state.set_state(UnifiedReply.waiting_for_input)
    sent = await message.answer("Перешли сообщение или просто вставь текст переписки")
    await state.update_data(setup_chat_id=sent.chat.id, setup_message_id=sent.message_id)


def unified_contacts_kb(contacts: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in contacts:
        b.button(text=_contact_name(c), callback_data=f"unified_contact:{c['id']}")
    b.button(text="➕ Другой человек", callback_data="unified_contact:new")
    b.adjust(1)
    return b.as_markup()


async def _edit_setup_message(
    state: FSMContext, bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Редактирует сообщение фазы настройки «Ответ с CueMe» (setup_chat_id/
    setup_message_id из FSM, см. _start_unified_reply) — общий хелпер для
    handle_unified_input/handle_unified_name, которые получают обычное
    message-событие (не callback), так что своего call.message для edit нет."""
    data = await state.get_data()
    chat_id, message_id = data.get("setup_chat_id"), data.get("setup_message_id")
    if chat_id is None or message_id is None:
        return  # не должно происходить — на всякий случай не роняем хендлер
    await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)


@router.message(UnifiedReply.waiting_for_input, _not_command)
async def handle_unified_input(message: Message, state: FSMContext, bot: Bot) -> None:
    # Функция «скриншот переписки → ответ» убрана целиком (по запросу) —
    # фото в этом состоянии больше не читаем через Vision, просто просим
    # текст. Раньше здесь был branch на message.photo → extract_chat_from_image.
    if message.photo:
        await _edit_setup_message(
            state, bot, "Скриншоты сейчас не поддерживаются — перешли сообщение или вставь текст.",
        )
        return

    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        await _edit_setup_message(state, bot, "Перешли сообщение или вставь текст.")
        return

    telegram_id = str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    await state.update_data(pending_text=incoming)

    if not contacts:
        await state.set_state(UnifiedReply.waiting_for_name)
        await _edit_setup_message(
            state, bot,
            "Как назвать этот диалог? Просто имя или метка, чтобы потом узнать среди контактов.",
        )
        return

    await _edit_setup_message(state, bot, "Кому отвечаем?", reply_markup=unified_contacts_kb(contacts))


@router.message(UnifiedReply.waiting_for_name)
async def handle_unified_name(message: Message, state: FSMContext, bot: Bot) -> None:
    name = (message.text or "").strip()
    if not name:
        await _edit_setup_message(state, bot, "Пришли имя текстом.")
        return

    data = await state.get_data()
    pending_text = data.get("pending_text")
    if not pending_text:
        await _edit_setup_message(state, bot, "Контекст устарел — начни заново через «💬 Ответ с CueMe».")
        await state.clear()
        return

    telegram_id = str(message.from_user.id)
    upsert_user(telegram_id, f"user{telegram_id}")
    contact_id = get_or_create_contact(telegram_id, f"live_{uuid.uuid4().hex}", name)

    await state.set_state(LiveDialogue.waiting_for_incoming)
    await state.update_data(contact_id=contact_id, dialogue_history=[])
    # "Готово — «name»." — часть той же редактируемой фазы настройки, не
    # отдельное сообщение (см. _start_unified_reply). Итог с вариантами
    # ответа дальше в _process_live_incoming — уже НОВОЕ сообщение.
    await _edit_setup_message(state, bot, f"Готово — «{name}». Генерирую варианты...")
    try:
        await _process_live_incoming(message, state, bot, pending_text, message.from_user.id)
    except Exception:
        # Страховка для новой автообработки первого сообщения (раньше юзер
        # мог остаться без ответа и без объяснения — см. фикс в
        # _process_live_incoming). Состояние уже LiveDialogue.waiting_for_incoming,
        # так что просто переслать сообщение ещё раз тоже сработает.
        logging.exception("handle_unified_name: сбой автообработки первого сообщения")
        await message.answer(
            "Не получилось обработать первое сообщение — пришли его ещё раз."
        )


@router.callback_query(F.data.startswith("unified_contact:"))
async def cb_unified_contact(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    raw_id = call.data.split(":", 1)[1]
    data = await state.get_data()
    pending_text = data.get("pending_text")
    if not pending_text:
        await call.answer("Контекст устарел — начни заново через «💬 Ответ с CueMe».", show_alert=True)
        return
    await call.answer()

    if raw_id == "new":
        await state.set_state(UnifiedReply.waiting_for_name)
        await call.message.edit_text(
            "Как назвать этот диалог? Просто имя или метка, чтобы потом узнать среди контактов."
        )
        return

    contact_id = int(raw_id)
    telegram_id = str(call.from_user.id)
    contact = get_contact_by_id(contact_id)
    if not contact:
        await call.answer("Контакт не найден.")
        return

    cards = await _ensure_reply_cards(call.message, telegram_id, contact_id, edit=True)
    if cards is None:
        return
    style_card, interaction_card = cards

    await state.update_data(
        style_card=style_card, interaction_card=interaction_card, contact_id=contact_id
    )
    await state.set_state(ReplyHelp.waiting_for_incoming)
    name = _contact_name(contact)
    await call.message.edit_text(f"Обрабатываю сообщение от {name}...")
    try:
        await _process_reply_incoming(call.message, state, bot, pending_text, call.from_user.id, edit=True)
    except Exception:
        logging.exception("cb_unified_contact: сбой автообработки первого сообщения")
        await call.message.answer(
            "Не получилось обработать первое сообщение — пришли его ещё раз."
        )


def _format_blocks(blocks: list[dict]) -> str:
    """Собирает блоки observation/mechanism/action в читаемое сообщение."""
    return "\n\n".join(
        f"🔍 {b['observation']}\n⚙️ {b['mechanism']}\n🎯 {b['action']}" for b in blocks
    )


# _last_incoming_line — использовалась только функцией «скриншот переписки →
# ответ» (распознанный OCR-текст скриншота, приближение последней реплики
# собеседника). Убрана вместе с ней, оставлена закомментированной на случай
# отката (см. секцию «Ответить по скриншоту» выше).
# def _last_incoming_line(chat_text: str) -> str:
#     """Последняя непустая строка распознанной переписки — приближение последней
#     реплики собеседника для ситуативной эвристики (скриншот/OCR). Если OCR
#     сохранил роли, пропускаем строки автора («Я: ...») и берём последнюю чужую."""
#     lines = [line.strip() for line in (chat_text or "").splitlines() if line.strip()]
#     if not lines:
#         return ""
#     self_re = re.compile(r"^(я|me|you)\s*[:：-]", re.IGNORECASE)
#     other_re = re.compile(r"^(собеседник|он|она|они|контакт|не я)\s*[:：-]", re.IGNORECASE)
#
#     for s in reversed(lines):
#         if other_re.match(s):
#             return s
#     for s in reversed(lines):
#         if not self_re.match(s):
#             return s
#     return lines[-1]


def _reply_data_signals(samples: dict | None, last_incoming: str) -> str | None:
    """Факты для промпта ответа (без LLM): стадия общения по объёму переписки +
    пометка о тяжёлой/сухой последней реплике. Готовый блок-список или None."""
    parts: list[str] = []
    if samples:
        # Стадия — по РЕАЛЬНОМУ объёму из features_summary; семплы усечены и годятся
        # лишь как фолбэк, если сводку не удалось распарсить.
        totals = totals_from_summary(samples.get("features_summary") or "")
        if totals:
            my_n, c_n = totals
        else:
            my_n = len(samples.get("my_sample") or [])
            c_n = len(samples.get("contact_sample") or [])
        if my_n + c_n >= 4:  # тот же порог, что и для разбора динамики
            parts.append(stage_hint(my_n, c_n))
    situ = detect_reply_situation(last_incoming)
    if situ:
        parts.append(situ)
    return "\n".join(f"• {p}" for p in parts) if parts else None


def _winning_for_contact(owner: str, contact_id) -> list[str] | None:
    """Few-shot «удачных заходов» автора с этим контактом (features.winning_messages
    по накопленной переписке). None, если контакта/данных нет — best-effort."""
    if not contact_id:
        return None
    try:
        wins = winning_messages(get_all_dated_messages(owner, contact_id))
    except Exception:
        logging.exception("winning: не удалось посчитать удачные заходы")
        return None
    return wins or None


async def _send_reply_analysis(message: Message, contact_id, incoming: str) -> None:
    """Короткий разбор динамики переписки перед выбором стиля.
    Дополняет готовый ответ, не заменяет его. При любой проблеме — молча пропускаем,
    чтобы не ломать основной flow ответа."""
    if not contact_id:
        return
    samples = get_message_samples(contact_id)
    if not samples:
        return
    my_sample      = samples["my_sample"] or []
    contact_sample = samples["contact_sample"] or []
    # Слишком мало сообщений — разбор был бы «на воде». Не тратим вызов LLM.
    if len(my_sample) + len(contact_sample) < 4:
        return
    try:
        blocks = await analyze_reply_dynamics(
            incoming,
            my_sample,
            contact_sample,
            samples["features_summary"],
        )
    except Exception:
        logging.exception("reply-analysis: не удалось сгенерировать разбор")
        return
    if blocks:
        await message.answer("🧭 Разбор переписки:\n\n" + _format_blocks(blocks))


_VARIANT_LETTERS = "АБВГДЕЁЖЗИ"


def _format_variants(variants: list[tuple[str, str]]) -> str:
    """HTML: текст каждого варианта в <code> — в Telegram такой блок копируется
    по одному тапу, без отдельной кнопки «Скопировать» на каждый вариант.
    Вступительная строка «Вот несколько вариантов...» убрана по запросу —
    варианты идут сразу; инсайт/инструкция про продолжение сессии теперь
    отдельным блоком снизу, см. _variants_footer."""
    blocks = []
    for i, (name, text) in enumerate(variants):
        letter = _VARIANT_LETTERS[i] if i < len(_VARIANT_LETTERS) else str(i + 1)
        blocks.append(
            f"<b>Вариант {letter}: {html.escape(name)}</b>\n"
            f"<code>{html.escape(text)}</code>"
        )
    return "\n\n".join(blocks)


def _variants_footer(insight: str | None, continuation: str) -> str:
    """Цитата (blockquote) внизу сообщения с вариантами — инсайт про
    собеседника (если есть — только у «живого» диалога, см. _run_live_coach_step)
    и инструкция про продолжение сессии. Строится ОДИН раз при первой
    генерации и сохраняется в ctx["footer_html"] — «Другие варианты»
    (force_fresh) переиспользует её как есть, меняются только сами варианты."""
    parts = []
    if insight:
        parts.append(html.escape(insight))
    parts.append(html.escape(continuation))
    return "<blockquote>" + "\n\n".join(parts) + "</blockquote>"


def _save_shown_suggestions(
    telegram_id: str, contact_id: int | None, kind: str, variants: list[tuple[str, str]],
) -> None:
    """Сохраняет тексты всех показанных пользователю вариантов — ДО того, как
    известно, использует он что-то из них (кандидаты на сопоставление с
    реальными исходящими, см. _match_outgoing_to_suggestion). Вызывать сразу
    при показе, для КАЖДОГО показа (включая «Другие варианты» — это тоже
    реально увиденные подсказки, могут быть использованы так же, как первые)."""
    try:
        save_suggestions(telegram_id, contact_id, kind, [text for _, text in variants])
    except Exception:
        logging.exception("suggestions: не удалось сохранить показанные варианты")


# _VARIANT_KINDS — какие ctx["kind"] поддерживают вариантную генерацию.
# «🎯 Другой тон» (точечный выбор одного стиля) убран — оставлена только
# перегенерация; вместе с ней ушла и старая style_pick_kb-инфраструктура.
# "screenshot" убран вместе с функцией «скриншот переписки → ответ» (см.
# закомментированную секцию «Ответить по скриншоту» выше).
_VARIANT_KINDS = ("reply",)


def variants_result_kb(action_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другие варианты", callback_data=f"varregen:{action_id}")
    return _with_back_to_menu(b.as_markup())


async def _run_variants_generation(
    target: Message, ctx: dict, telegram_id: int, bot: Bot, action_id: str,
    state: FSMContext | None = None, force_fresh: bool = False,
) -> None:
    """Общий шаг генерации нескольких именованных вариантов ОДНИМ вызовом LLM
    для «Ответ с CueMe» (kind всегда "reply" — kind="screenshot" убран вместе
    с функцией «скриншот переписки → ответ»). Гейт и списание триала — один
    раз за вызов (не за каждый вариант), т.к. это один вызов LLM."""
    kind = ctx.get("kind")
    text = ctx.get("text") if kind == "reply" else ctx.get("chat_text")
    if text is None:
        await target.answer("Контекст устарел — начни заново.")
        return

    style_card, interaction_card = ctx["style_card"], ctx["interaction_card"]
    signals = ctx.get("data_signals")
    winning = ctx.get("winning")
    gender = get_gender(str(telegram_id))
    cache_key = _style_cache_key(f"{kind}_variants", "", text, style_card, interaction_card, extra=gender or "")

    variants = None
    if not force_fresh:
        cached = get_llm_cache(cache_key, LLM_CACHE_TTL_SEC)
        if cached:
            try:
                variants = [tuple(v) for v in json.loads(cached)]
                logging.info("%s-variants: cache hit", kind)
            except (ValueError, TypeError):
                variants = None

    if variants is None:
        # Реальный вызов LLM — здесь и только здесь гейт + списание.
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        prev = ctx.get("variants") if force_fresh else None
        try:
            # kind всегда "reply" — "screenshot" убран вместе с функцией
            # «скриншот переписки → ответ» (screenshot_variants в llm.py
            # остался нетронутым, просто больше никем не вызывается).
            variants = await suggest_reply_variants(
                text, style_card, interaction_card,
                data_signals=signals, previous_variants=prev, winning_examples=winning,
                user_gender=gender,
            )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("%s-variants: ошибка генерации", kind)
            await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
            return

        # Успех — списываем ОДНУ попытку (не за каждый вариант — это один вызов
        # LLM) и кэшируем, даже если разбор дал меньше вариантов, чем просили.
        await _charge_trial_if_needed(bot, str(telegram_id))
        set_llm_cache(cache_key, json.dumps(variants, ensure_ascii=False))
        try:
            record_event(str(telegram_id), f"gen_{kind}_variants", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие генерации вариантов")

    if not variants:
        await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
        return

    _save_shown_suggestions(str(telegram_id), ctx.get("contact_id"), kind, variants)
    ctx["variants"] = variants

    # footer_html строится ОДИН раз (при первой генерации) и живёт в ctx —
    # «Другие варианты» (force_fresh) переиспользует его как есть, см.
    # _variants_footer. Результат — самостоятельное сообщение (не edit фазы
    # настройки, см. _start_unified_reply): первый раз answer (новое),
    # «Другие варианты» — edit того же результата.
    footer = ctx.get("footer_html")
    if footer is None:
        contact_gen, _ = _contact_words(gender)
        continuation = (
            f"Пришли следующее сообщение {contact_gen}, чтобы ответить и на него. "
            "Чтобы выйти из режима — нажми любую кнопку меню."
        )
        footer = _variants_footer(None, continuation)
        ctx["footer_html"] = footer

    text_out = f"{_format_variants(variants)}\n\n{footer}"
    if force_fresh:
        await _edit_or_answer_long(
            target, text_out, reply_markup=variants_result_kb(action_id), parse_mode="HTML",
        )
    else:
        await _answer_long(
            target, text_out, reply_markup=variants_result_kb(action_id), parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("varregen:"))
async def cb_variants_regen(call: CallbackQuery, state: FSMContext) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") not in _VARIANT_KINDS:
        await call.answer("Контекст устарел — начни заново.", show_alert=True)
        return
    await call.answer("Подбираю другие варианты...")
    await _run_variants_generation(call.message, ctx, call.from_user.id, call.bot, action_id, state, force_fresh=True)


async def _process_reply_incoming(
    message: Message, state: FSMContext, bot: Bot, incoming: str, user_id: int,
    edit: bool = False,
) -> None:
    """Общий хвост «Ответить за меня»: сборка ctx и генерация вариантов.
    user_id — ОТДЕЛЬНЫМ параметром (не message.from_user.id) — при вызове
    из callback-контекста message может быть call.message, чей .from_user
    это бот, не юзер (стандартная ловушка aiogram). edit=True — только из
    cb_unified_contact (message это call.message, только что отредактированное
    "Обрабатываю сообщение от {name}..." — сообщение БОТА): если триал уже
    исчерпан, пейволл редактирует его же, а не шлёт новое. edit=False (из
    handle_incoming, по умолчанию) — message это реальное пересланное
    сообщение от юзера, редактировать его нельзя."""
    telegram_id = str(user_id)
    data = await state.get_data()
    # Состояние НЕ сбрасываем — иначе следующее сообщение улетит в общий
    # авто-режим («Переписать») вместо продолжения «Ответить за меня».
    # Выйти из режима — любая кнопка меню (handle_menu_button сбрасывает state).

    contact_id = data.get("contact_id")
    if not await _quota_gate(bot, message, telegram_id, edit=edit):
        return

    # «Разбор переписки» (_send_reply_analysis) здесь отключён намеренно:
    # пользователь ждёт просто ответ, а не аналитику перед каждым ответом.
    # Вернуть — один вызов: await _send_reply_analysis(message, contact_id, incoming)
    samples = get_message_samples(contact_id) if contact_id else None
    ctx = {
        "kind": "reply", "text": incoming, "result": None, "style": None,
        "contact_id": contact_id,
        "style_card": data["style_card"], "interaction_card": data["interaction_card"],
        "data_signals": _reply_data_signals(samples, incoming),
        "winning": _winning_for_contact(telegram_id, contact_id),
    }
    action_id = _new_action(user_id, ctx)
    await _run_variants_generation(message, ctx, user_id, bot, action_id, state)


@router.message(ReplyHelp.waiting_for_incoming, _not_command)
async def handle_incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        await message.answer("Пришли сообщение собеседника текстом или голосовым.")
        return
    await _process_reply_incoming(message, state, bot, incoming, message.from_user.id)


@router.message(Command("reply"))
async def cmd_reply(message: Message, state: FSMContext) -> None:
    await _start_reply(message, state)


# ── 💫 Живой диалог с нуля (холодный старт, без порога накопления) ───────────

_LIVE_NEUTRAL_STYLE_PLACEHOLDER = (
    "Данных о твоём стиле письма пока нет — пиши так, как типично пишут в "
    "дейтинг-переписке в этом возрасте (18-30): на «ты», без канцелярита и "
    "лишней вежливости, чаще со строчной буквы в начале сообщения и без "
    "строгой пунктуации, разговорной длиной. Без домыслов о привычках автора "
    "сверх этого. Как только появятся другие данные (JSON-экспорт, другие "
    "переписки), стиль подключится сам и станет точнее."
)

# Тот же холодный старт, но для карточки собеседника — контакт мог
# автосоздаться от ОДНОГО исходящего business-сообщения (ещё до первого
# ответа от собеседника), тогда входящих сэмплов для интеракшн-карточки
# просто ещё нет. Раньше в этом случае «Ответить за меня» упирался в тупик
# («Не удалось сгенерировать анализ.») — теперь отвечаем нейтрально по
# смыслу присланного сообщения, без домыслов о манере письма собеседника.
_NEUTRAL_INTERACTION_PLACEHOLDER = (
    "Данных о стиле переписки собеседника пока нет (это первое сообщение с "
    "ним) — отвечай по смыслу присланного текста, без домыслов о его манере "
    "письма. Как только накопится история, бот подстроится точнее."
)

LIVE_NOTES_SUMMARY_EVERY = 4  # раз в сколько сообщений показывать «что я уже понял»


def _running_notes_preview(notes_text: str, n: int = 2) -> str:
    """Последние n непустых строк заметок — для короткого «что я уже понял»."""
    lines = [ln.strip() for ln in (notes_text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def live_variants_kb(action_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другие варианты", callback_data=f"liveregen:{action_id}")
    return _with_back_to_menu(b.as_markup())


# ── Готовые фразы (статичные скрипты, без LLM и без квоты) ────────────────────
# «Новый диалог» — развилка: 🎯 живой коучинг (существующий флоу) или 🎲 готовые
# открывашки (OPENERS_FOR_HER/HIM). «🔥 Скрипты общения» — отдельная кнопка меню
# с универсальными вопросами (REVIVE_QUESTIONS), работает для любого разговора.
# Показанные варианты в рамках сессии не повторяются (трекинг через FSM data,
# сбрасывается при исчерпании списка).

async def _pick_no_repeat(state: FSMContext, key: str, items: list[str]) -> str:
    """Случайный элемент items, не повторяющий уже показанные в этой сессии
    (индексы в FSM data[key]). Когда весь список исчерпан — начинает заново."""
    data = await state.get_data()
    shown = data.get(key) or []
    remaining = [i for i in range(len(items)) if i not in shown]
    if not remaining:
        remaining = list(range(len(items)))
        shown = []
    idx = random.choice(remaining)
    await state.update_data(**{key: shown + [idx]})
    return items[idx]


def _copy_block(intro: str, phrase: str, kb: InlineKeyboardMarkup) -> tuple[str, dict]:
    """Одна фраза tap-to-copy (HTML <code>) + интро + кнопка «Другой вариант»."""
    text = f"{intro}\n\n<code>{html.escape(phrase)}</code>"
    return text, {"reply_markup": kb, "parse_mode": "HTML"}


# --- Новый диалог: развилка коучинг / готовые фразы ---

def live_start_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Живой коучинг", callback_data="live:coach")
    b.button(text="🎲 Готовые фразы для начала", callback_data="live:phrases")
    b.adjust(1)
    return b.as_markup()


async def _show_live_start(message: Message) -> None:
    await message.answer("Как начнём?", reply_markup=live_start_kb())


@router.callback_query(F.data == "live:coach")
async def cb_live_coach(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _start_live_dialogue(call.message, state)


def phrases_gender_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👩 Ей", callback_data="phrases:her")
    b.button(text="👨 Ему", callback_data="phrases:him")
    b.adjust(2)
    return b.as_markup()


def phrase_next_kb(target: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другой вариант", callback_data=f"phrase_next:{target}")
    return b.as_markup()


async def _send_opener(msg: Message, state: FSMContext, target: str) -> None:
    items = OPENERS_FOR_HER if target == "her" else OPENERS_FOR_HIM
    phrase = await _pick_no_repeat(state, f"opener_shown_{target}", items)
    intro = "Держи заход (тапни, чтобы скопировать):"
    if "[" in phrase:
        intro += " замени [то, что в скобках] на реальную деталь из анкеты."
    text, kw = _copy_block(intro, phrase, phrase_next_kb(target))
    await msg.answer(text, **kw)


@router.callback_query(F.data == "live:phrases")
async def cb_live_phrases(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer("Кому пишешь?", reply_markup=phrases_gender_kb())


@router.callback_query(F.data.startswith("phrases:"))
async def cb_phrases_gender(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]  # her | him
    await call.answer()
    await _send_opener(call.message, state, target)


@router.callback_query(F.data.startswith("phrase_next:"))
async def cb_phrase_next(call: CallbackQuery, state: FSMContext) -> None:
    target = call.data.split(":", 1)[1]
    await call.answer("Другой вариант")
    await _send_opener(call.message, state, target)


# --- 🔥 Скрипты общения (универсальные вопросы, отдельная кнопка) ---

def revive_next_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другой вариант", callback_data="revive_next")
    return b.as_markup()


async def _send_revive(msg: Message, state: FSMContext) -> None:
    q = await _pick_no_repeat(state, "revive_shown", REVIVE_QUESTIONS)
    text, kw = _copy_block("Вот что может оживить разговор:", q, revive_next_kb())
    await msg.answer(text, **kw)


async def _show_revive(message: Message, state: FSMContext) -> None:
    await _send_revive(message, state)


@router.callback_query(F.data == "revive_next")
async def cb_revive_next(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("Другой вариант")
    await _send_revive(call.message, state)


async def _start_live_dialogue(message: Message, state: FSMContext) -> None:
    await state.set_state(LiveDialogue.waiting_for_name)
    await message.answer(
        "Как назвать этот диалог? Просто имя или метка, чтобы потом узнать среди контактов."
    )


@router.message(LiveDialogue.waiting_for_name)
async def handle_live_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пришли имя текстом.")
        return

    telegram_id = str(message.from_user.id)
    upsert_user(telegram_id, f"user{telegram_id}")
    contact_id = get_or_create_contact(telegram_id, f"live_{uuid.uuid4().hex}", name)

    await state.set_state(LiveDialogue.waiting_for_incoming)
    await state.update_data(contact_id=contact_id, dialogue_history=[])
    gender = get_gender(telegram_id)
    _, pron = _contact_words(gender)
    await message.answer(
        f"Готово — «{name}». Присылай {pron} сообщения по одному, на каждое сразу дам "
        "несколько вариантов ответа. Чтобы выйти из режима — нажми любую кнопку меню."
    )


async def _process_live_incoming(
    message: Message, state: FSMContext, bot: Bot, incoming: str, user_id: int,
) -> None:
    """Общий хвост «Живого диалога»: сборка ctx и live-коучинг. user_id —
    отдельным параметром, см. комментарий в _process_reply_incoming."""
    telegram_id = str(user_id)
    data = await state.get_data()
    # Состояние НЕ сбрасываем — можно форвардить сообщения одно за другим без
    # повторного нажатия кнопки. Выйти из режима — любая кнопка меню.

    if not await _quota_gate(bot, message, telegram_id):
        return

    contact_id = data.get("contact_id")
    if not contact_id:
        await message.answer("Контекст диалога потерян — начни заново через «💬 Ответ с CueMe».")
        return

    # В отличие от остальных LLM-вызовов в файле, раньше был без try/except —
    # сбой провайдера (рейтлимит/таймаут) тут тихо убивал автообработку
    # первого сообщения сразу после создания контакта: юзер видел "Готово —
    # «имя»." и дальше тишину, без единого намёка на ошибку.
    try:
        style_card = await _gen_style_card(telegram_id) or _LIVE_NEUTRAL_STYLE_PLACEHOLDER
    except RateLimitError:
        await message.answer("Лимит запросов исчерпан — попробуй через пару минут.")
        return
    except Exception:
        logging.exception("_process_live_incoming: не удалось получить стиль")
        await message.answer("Сервис сейчас перегружен — попробуй чуть позже.")
        return
    notes_row = get_running_notes(contact_id)
    running_notes = notes_row["notes_text"] if notes_row else None
    message_count = notes_row["message_count"] if notes_row else 0
    dialogue_history = data.get("dialogue_history") or []

    ctx = {
        "kind": "live", "text": incoming, "contact_id": contact_id,
        "style_card": style_card, "running_notes": running_notes,
        "dialogue_history": dialogue_history, "message_count": message_count,
        "variants": None,
    }
    action_id = _new_action(user_id, ctx)

    # Короткая история диалога — эфемерно, в FSM; долгая память — running_notes в БД.
    new_history = (dialogue_history + [incoming])[-8:]
    await state.update_data(dialogue_history=new_history)

    await _run_live_coach_step(message, ctx, user_id, bot, action_id)


@router.message(LiveDialogue.waiting_for_incoming, _not_command)
async def handle_live_incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    txt, _ = await _message_text(bot, message)
    incoming = (txt or "").strip()
    if not incoming:
        contact_gen, _ = _contact_words(get_gender(str(message.from_user.id)))
        await message.answer(f"Пришли сообщение {contact_gen} текстом или голосовым.")
        return
    await _process_live_incoming(message, state, bot, incoming, message.from_user.id)


async def _run_live_coach_step(
    target: Message, ctx: dict, telegram_id: int, bot: Bot, action_id: str, force_fresh: bool = False,
) -> None:
    """«Живой диалог»: первый проход — live_coach_step (советы + допись заметок,
    одна попытка триала на пересланное сообщение). «Другие варианты» — просто
    suggest_reply_variants поверх уже сохранённых заметок, БЕЗ повторной записи
    в running_notes (иначе один и тот же инсайт задвоился бы в заметках)."""
    text = ctx.get("text")
    contact_id = ctx.get("contact_id")
    if text is None or not contact_id:
        await target.answer("Контекст устарел — начни заново.")
        return

    style_card = ctx["style_card"]
    running_notes = ctx.get("running_notes") or ""
    gender = get_gender(str(telegram_id))

    if force_fresh:
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        try:
            variants = await suggest_reply_variants(
                text, style_card, running_notes, previous_variants=ctx.get("variants"),
                user_gender=gender,
            )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("live-coach: ошибка регена вариантов")
            await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
            return
        await _charge_trial_if_needed(bot, str(telegram_id))
        try:
            record_event(str(telegram_id), "gen_live_regen", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие live-регена")
        if not variants:
            await target.answer("Не получилось сгенерировать варианты — попробуй ещё раз.")
            return
        _save_shown_suggestions(str(telegram_id), contact_id, "live", variants)
        ctx["variants"] = variants
        # footer_html уже посчитан при первой генерации (см. конец функции) и
        # хранится в ctx — «Другие варианты» его не пересчитывает, инсайт и
        # инструкция остаются как были, меняются только сами варианты. Editим
        # ЭТО ЖЕ сообщение (target = call.message из cb_live_regen), а не
        # шлём новое.
        footer = ctx.get("footer_html", "")
        await _edit_or_answer_long(
            target, f"{_format_variants(variants)}\n\n{footer}",
            reply_markup=live_variants_kb(action_id), parse_mode="HTML",
        )
        return

    cache_key = _style_cache_key("live", "", text, style_card, running_notes, extra=gender or "")
    cached = get_llm_cache(cache_key, LLM_CACHE_TTL_SEC)
    variants = updated_notes = None
    if cached:
        try:
            payload = json.loads(cached)
            variants = [tuple(v) for v in payload["variants"]]
            updated_notes = payload["notes"]
            logging.info("live-coach: cache hit")
        except (ValueError, TypeError, KeyError):
            variants = updated_notes = None

    if variants is None:
        if not await _quota_gate(bot, target, str(telegram_id)):
            return
        try:
            variants, updated_notes = await live_coach_step(
                text, style_card, running_notes or None, ctx.get("dialogue_history"),
                user_gender=gender,
            )
        except RateLimitError:
            await target.answer("Лимит исчерпан, попробуй позже.")
            return
        except Exception:
            logging.exception("live-coach: ошибка генерации")
            await target.answer("Не получилось сгенерировать совет — попробуй ещё раз.")
            return

        # Успех — списываем ОДНУ попытку (один вызов LLM даёт и советы, и заметки).
        await _charge_trial_if_needed(bot, str(telegram_id))
        set_llm_cache(cache_key, json.dumps({"variants": variants, "notes": updated_notes}, ensure_ascii=False))
        try:
            record_event(str(telegram_id), "gen_live", str(len(variants)))
        except Exception:
            logging.exception("telemetry: не удалось записать событие live-генерации")

        new_count = ctx.get("message_count", 0) + 1
        save_running_notes(contact_id, updated_notes, new_count)
        ctx["message_count"] = new_count

    if not variants:
        await target.answer("Не получилось сгенерировать совет — попробуй ещё раз.")
        return

    _save_shown_suggestions(str(telegram_id), contact_id, "live", variants)
    ctx["variants"] = variants
    ctx["running_notes"] = updated_notes

    # Инсайт («Что я уже понял») и инструкция про продолжение сессии — ОДНОЙ
    # цитатой внизу итогового сообщения (не отдельными message), см.
    # _variants_footer. footer_html сохраняется в ctx — «Другие варианты»
    # (force_fresh, ветка выше) переиспользует его без изменений.
    message_count = ctx.get("message_count", 0)
    insight = None
    if updated_notes and (message_count == 1 or message_count % LIVE_NOTES_SUMMARY_EVERY == 0):
        preview = _running_notes_preview(updated_notes)
        if preview:
            insight = f"Что я уже понял:\n{preview}"

    contact_gen, _ = _contact_words(gender)
    continuation = (
        f"Пришли следующее сообщение {contact_gen} — отвечу и на него. "
        "Чтобы выйти из режима — нажми любую кнопку меню."
    )
    footer = _variants_footer(insight, continuation)
    ctx["footer_html"] = footer

    # Итог — САМОСТОЯТЕЛЬНОЕ сообщение (не edit фазы настройки, см.
    # _start_unified_reply/_edit_setup_message): answer шлёт НОВОЕ.
    await _answer_long(
        target, f"{_format_variants(variants)}\n\n{footer}",
        reply_markup=live_variants_kb(action_id), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("liveregen:"))
async def cb_live_regen(call: CallbackQuery) -> None:
    action_id = call.data.split(":", 1)[1]
    ctx = _get_action(call.from_user.id, action_id)
    if not ctx or ctx.get("kind") != "live":
        await call.answer("Контекст устарел — начни заново через «💫 Новый диалог».", show_alert=True)
        return
    await call.answer("Подбираю другие варианты...")
    await _run_live_coach_step(call.message, ctx, call.from_user.id, call.bot, action_id, force_fresh=True)


# ── 📸 Ответить по скриншоту — УБРАНА ЦЕЛИКОМ по запросу пользователя ────────
# Отдельная команда /screenshot (в дополнение к общей точке входа «💬 Ответ
# с CueMe», которая тоже больше не принимает фото — см. handle_unified_input
# выше). Закомментирована целиком, не удалена физически — на случай отката.
# Использовала kind="screenshot" в _run_variants_generation (screenshot_variants
# в llm.py) — та ветка тоже закомментирована ниже, вместе с Screenshot(FSM).

# async def _start_screenshot(message: Message, state: FSMContext) -> None:
#     telegram_id = str(message.from_user.id)
#     if not list_contacts(telegram_id):
#         await _send_no_contacts_hint(message)
#         return
#     await state.set_state(Screenshot.waiting_for_image)
#     await message.answer("Пришли скриншот переписки (или вставь текст диалога), на который нужно ответить:")
#
#
# @router.message(Command("screenshot"))
# async def cmd_screenshot(message: Message, state: FSMContext) -> None:
#     await _start_screenshot(message, state)
#
#
# @router.message(Screenshot.waiting_for_image, F.photo)
# async def handle_screenshot_photo(message: Message, state: FSMContext, bot: Bot) -> None:
#     await message.answer("Читаю скриншот...")
#     try:
#         buf = await bot.download(message.photo[-1])
#         chat_text = await extract_chat_from_image(buf.read())
#     except Exception:
#         logging.exception("screenshot: не удалось скачать/распознать")
#         chat_text = ""
#
#     if not chat_text or chat_text.strip() == ILLEGIBLE_MARKER:
#         await message.answer("Не смог прочитать скриншот — пришли текст переписки сообщением.")
#         return  # остаёмся в Screenshot.waiting_for_image
#
#     await _proceed_screenshot_style_pick(message, state, chat_text)
#
#
# @router.message(Screenshot.waiting_for_image, F.text)
# async def handle_screenshot_text(message: Message, state: FSMContext) -> None:
#     chat_text = (message.text or "").strip()
#     if not chat_text:
#         await message.answer("Пришли скриншот или текст переписки.")
#         return
#     await _proceed_screenshot_style_pick(message, state, chat_text)
#
#
# def screenshot_contact_pick_kb(contacts: list, action_id: str) -> InlineKeyboardMarkup:
#     """Как contacts_kb, но с кнопкой для человека, которого ещё нет в базе —
#     для него используется общий (агрегатный) стиль, без interaction_card."""
#     b = InlineKeyboardBuilder()
#     for c in contacts:
#         b.button(text=_contact_name(c), callback_data=f"shotcontact:{c['id']}:{action_id}")
#     b.button(text="🆕 Новый человек (нет в базе)", callback_data=f"shotcontact:new:{action_id}")
#     b.adjust(1)
#     return b.as_markup()
#
#
# async def _proceed_screenshot_style_pick(message: Message, state: FSMContext, chat_text: str) -> None:
#     await state.clear()
#     telegram_id = str(message.from_user.id)
#     contacts = list_contacts(telegram_id)
#
#     action_id = _new_action(message.from_user.id, {"kind": "screenshot_pending", "chat_text": chat_text})
#     await message.answer("Чья это переписка?", reply_markup=screenshot_contact_pick_kb(contacts, action_id))
#
#
# @router.callback_query(F.data.startswith("shotcontact:"))
# async def cb_screenshot_contact(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
#     parts = call.data.split(":")
#     if len(parts) != 3:
#         await call.answer("Контекст устарел — начни заново через «📸 По скриншоту».", show_alert=True)
#         return
#     _, raw_id, action_id = parts
#     telegram_id = str(call.from_user.id)
#
#     ctx = _get_action(call.from_user.id, action_id)
#     if not ctx or ctx.get("kind") != "screenshot_pending":
#         await call.answer("Контекст устарел — начни заново через «📸 По скриншоту».", show_alert=True)
#         return
#
#     if raw_id == "new":
#         await call.answer()
#         await _prompt_screenshot_style_no_contact(bot, call.message, call.from_user.id, telegram_id, ctx["chat_text"], state, edit=True)
#         return
#
#     contact_id = int(raw_id)
#     contact = get_contact_by_id(contact_id)
#     if not contact:
#         await call.answer("Контакт не найден.")
#         return
#
#     await call.answer()
#     await _prompt_screenshot_style(bot, call.message, call.from_user.id, telegram_id, contact_id, ctx["chat_text"], state, edit=True)
#
#
# async def _prompt_screenshot_style(
#     bot: Bot, target: Message, user_id: int, telegram_id: str, contact_id: int, chat_text: str,
#     state: FSMContext, edit: bool = False,
# ) -> None:
#     # ВАЖНО: user_id передаётся отдельным параметром, а не берётся из
#     # target.from_user — при edit=True target это call.message, чей
#     # .from_user это БОТ, а не пользователь (стандартная ловушка aiogram).
#     if not await _quota_gate(bot, target, telegram_id):
#         return
#     # Генерация карточек ходит в LLM — без обработки ошибок сбой (лимит/провайдер
#     # недоступен) тихо убивал кнопку: спиннер гас, а сообщение не менялось.
#     try:
#         style_card = await _style_for_rewrite(telegram_id, contact_id)
#         interaction_card = (await _gen_interaction_card(contact_id, telegram_id) or "") if style_card else ""
#     except RateLimitError:
#         await (target.edit_text if edit else target.answer)("Лимит запросов исчерпан — попробуй через пару минут.")
#         return
#     except Exception:
#         logging.exception("screenshot: не удалось сгенерировать карточки")
#         await (target.edit_text if edit else target.answer)("Сервис сейчас перегружен — попробуй чуть позже.")
#         return
#     if not style_card:
#         text = "Не удалось получить твой стиль — сначала загрузи JSON чата или дай накопить сообщений."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#
#     samples = get_message_samples(contact_id)
#     ctx = {
#         "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
#         "contact_id": contact_id,
#         "style_card": style_card, "interaction_card": interaction_card,
#         "data_signals": _reply_data_signals(samples, _last_incoming_line(chat_text)),
#         "winning": _winning_for_contact(telegram_id, contact_id),
#     }
#     action_id = _new_action(user_id, ctx)
#     if edit:
#         await target.edit_text("Генерирую варианты...")
#     else:
#         await target.answer("Генерирую варианты...")
#     await _run_variants_generation(target, ctx, user_id, bot, action_id, state)
#
#
# async def _prompt_screenshot_style_no_contact(
#     bot: Bot, target: Message, user_id: int, telegram_id: str, chat_text: str,
#     state: FSMContext, edit: bool = False,
# ) -> None:
#     """Для человека, которого ещё нет в базе — общий (агрегатный) стиль автора,
#     без per-contact interaction_card (промпт сам подставит нейтральный фолбэк)."""
#     if not await _quota_gate(bot, target, telegram_id):
#         return
#     try:
#         style_card = await _gen_style_card(telegram_id)
#     except RateLimitError:
#         await (target.edit_text if edit else target.answer)("Лимит запросов исчерпан — попробуй через пару минут.")
#         return
#     except Exception:
#         logging.exception("screenshot(new): не удалось сгенерировать стиль")
#         await (target.edit_text if edit else target.answer)("Сервис сейчас перегружен — попробуй чуть позже.")
#         return
#     if not style_card:
#         text = "Не удалось получить твой стиль — сначала загрузи JSON чата или дай накопить сообщений."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#
#     ctx = {
#         "kind": "screenshot", "chat_text": chat_text, "result": None, "style": None,
#         "style_card": style_card, "interaction_card": "",
#         "data_signals": _reply_data_signals(None, _last_incoming_line(chat_text)),
#     }
#     action_id = _new_action(user_id, ctx)
#     if edit:
#         await target.edit_text("Генерирую варианты...")
#     else:
#         await target.answer("Генерирую варианты...")
#     await _run_variants_generation(target, ctx, user_id, bot, action_id, state)
