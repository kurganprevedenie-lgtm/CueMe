"""«🔬 Анализ собеседника» и «💐 Идеальное свидание».

Вынесено из main.py без изменения логики (строки 3821–5088 исходного файла).
Порядок регистрации хендлеров задаёт main.py порядком импорта модулей —
этот модуль нельзя импортировать раньше предыдущих (см. tests/test_handler_registry.py)."""
import html
import json
import logging
import random
import re
from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InputRichMessage,
    Message,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import REBUILD_THRESHOLD
from llm import RateLimitError, build_compatibility_interpretation, build_ideal_date
from compatibility_metrics import compute_all as compute_compat_metrics
from storage import (
    count_biz_messages_for_contact,
    count_imported_messages,
    delete_deep_analysis,
    delete_ideal_date,
    get_all_dated_messages,
    get_contact_by_id,
    get_analysis_trial_count,
    get_date_trial_count,
    get_analysis_trial_bonus,
    get_date_trial_bonus,
    get_deep_analysis,
    get_gender,
    get_ideal_date,
    mark_analysis_trial_used,
    mark_date_trial_used,
    list_contacts,
    save_deep_analysis,
    save_ideal_date,
    record_event,
)
from app_state import (
    _PROGRESS_FRAMES,
    _contact_name,
    _edit_or_answer_long,
    _edit_or_keep,
    dp,
    with_progress_animation,
)
from handlers.subscription import (
    _charge_feature_trial_if_needed,
    _is_premium,
    _never_unlocked,
    _note_outcome,
    _record_gate_event,
    _retry_kb,
    _unlock_gate_kb,
    _unlock_gate_text,
)
from handlers.redeem_menu import _with_back_to_menu
from handlers.profile import contacts_kb
from handlers.cards import _gen_interaction_card
import handlers  # ссылки по модулю: имена ниже по исходному main.py / общее состояние


# ── 🔬 Анализ собеседника ─────────────────────────────────────────────────────

DEEP_ANALYSIS_MIN_MSGS = 10  # минимум сообщений с каждой стороны, иначе анализ бессмысленен


_JUNK_WORDS = {"чо", "лол", "кек", "рофл", "ору", "угар", "мда", "эм", "ауф", "хм"}


def _is_junk_message(text: str) -> bool:
    """Мусор для целей ЦИТИРОВАНИЯ в LLM-анализе (build_deep_analysis) — смех/
    междометия, голая пунктуация/эмодзи, куцые реакции без контекста типа
    "сукаааа"/"лол"/"чо". Само сообщение из данных не убирается (см.
    _periodized_dated_lines) — только помечается как непригодное для цитаты,
    чтобы модель не приводила его как иллюстративный «бид»/пример. Короткие, но
    содержательные ответы («да», «нет», «ок») мусором НЕ считаются — это
    реальные прямые ответы (см. правку про «уход от прямого ответа»)."""
    t = text.strip()
    if len(t) <= 1:
        return True
    if not re.search(r"[a-zA-Zа-яА-ЯёЁ0-9]", t):
        return True  # только пунктуация/эмодзи, ни одной буквы или цифры
    if " " in t:
        return False  # многословные сообщения не считаем голой репликой
    low = t.lower().strip(" !?.,)(")
    if not low:
        return True
    if low in _JUNK_WORDS:
        return True
    # длинный прогон одного символа с коротким "корнем" — сукаааа, нееееет,
    # дааааа: схлопываем 3+ повторов подряд и смотрим, что осталось
    collapsed = re.sub(r"(.)\1{2,}", r"\1", low)
    if len(collapsed) <= 4 and len(low) >= 6:
        return True
    # смеховое чередование ха/ах/хи/хо и т.п. — считаем по доле покрытия, а не
    # точным совпадением целиком, чтобы ловить и захламлённые вставными буквами
    # варианты вроде "хехахааххааххаппхахахаха"
    if len(low) >= 4:
        hits = len(re.findall(r"ха|ах|хи|их|хе|ех|хо|ох", low))
        if hits * 2 / len(low) >= 0.6:
            return True
    return False


def _periodized_dated_lines(rows: list[dict], target_total: int = 220, buckets: int = 6) -> list[str]:
    """Хронологический семпл с равномерным охватом всей истории (не только
    последних сообщений) — бьём на буквенных бакетов по времени и берём
    равномерные срезы внутри каждого, чтобы LLM видел динамику по периодам."""
    rows = sorted((r for r in rows if r["text"] and r["text"].strip()), key=lambda r: r["date"])
    if not rows:
        return []

    per_bucket  = max(1, target_total // buckets)
    bucket_size = max(1, len(rows) // buckets)
    lines: list[str] = []
    for i in range(0, len(rows), bucket_size):
        chunk = rows[i:i + bucket_size]
        step  = max(1, len(chunk) // per_bucket)
        for r in chunk[::step][:per_bucket]:
            who = "Я" if r["direction"] == "out" else "Собеседник"
            when = r["date"][:16].replace("T", " ")
            tag  = " [шум, не цитировать]" if _is_junk_message(r["text"]) else ""
            lines.append(f"{when} {who}: {r['text']}{tag}")
    return lines


def _deep_stats_summary(rows: list[dict]) -> str:
    my = [r for r in rows if r["direction"] == "out" and r["text"]]
    ct = [r for r in rows if r["direction"] == "in" and r["text"]]
    dates = sorted(r["date"] for r in rows if r["text"])
    date_from = dates[0][:10] if dates else "?"
    date_to   = dates[-1][:10] if dates else "?"
    my_avg = sum(len(t["text"]) for t in my) / len(my) if my else 0
    ct_avg = sum(len(t["text"]) for t in ct) / len(ct) if ct else 0
    return (
        f"Период переписки: {date_from} — {date_to}\n"
        f"Я: {len(my)} сообщ., средн. {my_avg:.0f} симв.\n"
        f"Собеседник: {len(ct)} сообщ., средн. {ct_avg:.0f} симв."
    )


# v1 (совместимость/история по периодам/флаги/подарки, 3 сообщения msg1/msg2/msg3
# с отдельным interaction_card) — оставлено для отката. Заменено 4-блочной
# структурой (совместимость/как писать/длина-ритм-регистр/флаги), которая сама
# закрывает то, что раньше показывал отдельный interaction_card-вызов в msg2.
# async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
#     """Ленивая генерация с кэшем в deep_analysis. None — данных мало."""
#     cached = get_deep_analysis(contact_id)
#     if cached:
#         return cached
#
#     rows = get_all_dated_messages(owner_user_id, contact_id)
#     my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
#     ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
#     if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
#         return None
#
#     dated_lines = _periodized_dated_lines(rows)
#     stats       = _deep_stats_summary(rows)
#     compat, history, swot, gifts = await build_deep_analysis(
#         dated_lines, stats, user_gender=get_gender(owner_user_id),
#     )
#     save_deep_analysis(contact_id, compat, history, swot, gifts)
#     return {
#         "compatibility_text": compat, "history_text": history,
#         "swot_text": swot, "gifts_text": gifts,
#     }
#
#
# def _format_deep_analysis(name: str, data: dict, interaction_card: str | None) -> tuple[str, str, str]:
#     msg1 = (
#         f"🔬 Анализ собеседника — {name}\n\n"
#         f"💞 Совместимость\n\n{data['compatibility_text']}\n\n"
#         f"📖 История отношений\n\n{data['history_text']}"
#     )
#     msg2 = f"🗣️ Стиль и привычки {name}\n\n{interaction_card}" if interaction_card else ""
#     msg3 = (
#         f"🚩💚 Флаги\n\n{data['swot_text']}\n\n"
#         f"🎁 Рекомендации подарков\n\n{data['gifts_text']}"
#     )
#     return msg1, msg2, msg3


# v-5axis (единый текст «Название: N/5», медаль 0-25, парсинг текста обратно
# для Rich Message) — оставлено для отката. Заменено детерминированными
# метриками (compatibility_metrics.py) + LLM только для интерпретации уже
# посчитанных фактов (build_compatibility_interpretation в llm.py) — не нужен
# весь текст переписки в промпте, только 7 готовых фактов, поэтому быстрее и
# не рискует таймаутом на больших контактах. См. новые версии ниже.
# async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
#     """Ленивая генерация с кэшем в deep_analysis. None — данных мало."""
#     cached = get_deep_analysis(contact_id)
#     if cached:
#         return cached

#     rows = get_all_dated_messages(owner_user_id, contact_id)
#     my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
#     ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
#     if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
#         return None

#     dated_lines = _periodized_dated_lines(rows)
#     stats       = _deep_stats_summary(rows)
#     compat = await build_deep_analysis(
#         dated_lines, stats, rows, user_gender=get_gender(owner_user_id),
#     )
#     save_deep_analysis(contact_id, compat)
#     return {"compatibility_text": compat}


# def _format_deep_analysis(name: str, data: dict) -> str:
#     """Единый блок — 5 осей с обоснованием вместо прежних 4 разрозненных
#     блоков (совместимость/как писать/флаги/готовое сообщение — «как писать» и
#     «флаги» пересказывали то же самое, что теперь показывают оси; «готовое
#     сообщение» дублировало отдельную функцию «Ответить за меня», убрано без
#     замены). Разбивку на несколько сообщений при превышении лимита Telegram
#     делает _answer_long — она режет по границам абзацев, тут не нужно.
#     Это plain-text ФОЛБЭК для _run_deep_analysis — основной путь теперь Rich
#     Message (см. _parse_compat_text/_build_rich_analysis_html), этот формат
#     остаётся на случай, если Rich Message не отправился."""
#     return f"🔬 Анализ собеседника — {name}\n\n{data['compatibility_text']}"


# def _parse_compat_text(compatibility_text: str) -> tuple[str, list[tuple[str, int, str]], str] | None:
#     """Разбирает уже готовый текст build_deep_analysis (медаль+сумма, 5 осей
#     «Название: N/5» + обоснование, финальная строка 👉 совет) обратно на
#     структурные куски для Rich Message. build_deep_analysis НЕ меняется —
#     это чисто раскладка уже сгенерированного текста, не новая генерация.
#     None, если структура неожиданная (не 5 осей/нет совета) — сигнал сразу
#     уйти в текстовый фолбэк, не пытаясь звать Rich Message API вслепую."""
#     lines = compatibility_text.splitlines()
#     if not lines:
#         return None
#     medal_line = lines[0].strip()
#     axes: list[tuple[str, int, str]] = []
#     advice = ""
#     i = 1
#     n = len(lines)
#     while i < n:
#         line = lines[i].strip()
#         if not line:
#             i += 1
#             continue
#         m = _AXIS_HEADER_RE.match(line)
#         if m:
#             axis_name, score = m.group(1), int(m.group(2))
#             i += 1
#             body_lines = []
#             while i < n and lines[i].strip() and not _AXIS_HEADER_RE.match(lines[i].strip()):
#                 body_lines.append(lines[i].strip())
#                 i += 1
#             axes.append((axis_name, score, " ".join(body_lines)))
#             continue
#         if line.startswith("👉"):
#             advice = line.lstrip("👉").strip()
#             i += 1
#             continue
#         i += 1  # неожиданная строка — пропускаем, не валим весь парсинг

#     if len(axes) != 5 or not advice:
#         return None
#     return medal_line, axes, advice


# def _short_phrase(text: str, max_len: int = 55) -> str:
#     """Первое предложение уже готового текста оси, обрезанное по длине —
#     компактная «суть» для ячейки таблицы. Без LLM: не просим модель отдельно
#     генерировать короткую версию (нет возможности прогнать живой тест на
#     промпт прямо сейчас), просто урезаем то, что она уже написала."""
#     text = text.strip()
#     if not text:
#         return "—"
#     m = re.match(r"(.+?[.!?])(?:\s|$)", text)
#     first = m.group(1) if m else text
#     if len(first) > max_len:
#         first = first[: max_len - 1].rstrip() + "…"
#     return first


# def _build_rich_analysis_html(
#     name: str, medal_line: str, axes: list[tuple[str, int, str]], advice: str,
# ) -> str:
#     """HTML для sendRichMessage (Bot API 10.1+, aiogram InputRichMessage.html):
#     таблица с 5 баллами сразу видна, полное обоснование — в <details> без
#     open (свёрнуто по умолчанию), совет — <mark> акцентом."""
#     esc = html.escape
#     rows = "\n".join(
#         f'<tr><td align="left">{esc(axis_name)}</td>'
#         f'<td align="center">{score}/5</td>'
#         f'<td align="left">{esc(_short_phrase(body))}</td></tr>'
#         for axis_name, score, body in axes
#     )
#     detail_paras = "\n".join(
#         f"<p><b>{esc(axis_name)}</b> — {score}/5. {esc(body)}</p>"
#         for axis_name, score, body in axes
#     )
#     return (
#         f"<h2>🔬 Анализ собеседника — {esc(name)}</h2>\n"
#         f"<h3>{esc(medal_line)}</h3>\n"
#         "<table>\n"
#         '<tr><th align="left">Показатель</th><th align="center">Балл</th>'
#         '<th align="left">Суть</th></tr>\n'
#         f"{rows}\n"
#         "</table>\n"
#         "<details>\n"
#         "<summary>Показать обоснование</summary>\n"
#         f"{detail_paras}\n"
#         "</details>\n"
#         f"<p><mark>👉 {esc(advice)}</mark></p>"
#     )


# def deep_analysis_result_kb(contact_id: int) -> InlineKeyboardMarkup:
#     b = InlineKeyboardBuilder()
#     b.button(text="🔄 Обновить анализ", callback_data=f"deepan_refresh:{contact_id}")
#     return b.as_markup()


# async def _run_deep_analysis(
#     bot: Bot, target: Message, telegram_id: str, contact_id: int, edit: bool = False
# ) -> None:
#     # Реферальная награда теперь даёт полный Premium (учтено внутри _is_premium,
#     # которую вызывает _require_premium) — отдельной проверки тут больше не нужно.
#     if not await _require_premium(bot, target, telegram_id):
#         return
#     contact = get_contact_by_id(contact_id)
#     if not contact:
#         text = "Контакт не найден."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#     name = _contact_name(contact)

#     wait_text = f"Готовлю анализ собеседника — {name}. Это займёт ~30 секунд..."
#     await (target.edit_text(wait_text) if edit else target.answer(wait_text))

#     try:
#         data = await _gen_deep_analysis(contact_id, telegram_id)
#     except RateLimitError:
#         await target.answer("Лимит LLM исчерпан, попробуй позже.")
#         return
#     except Exception:
#         logging.exception("deep_analysis: ошибка генерации")
#         await target.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
#         return

#     if not data:
#         await target.answer(
#             f"Пока маловато данных по {name} для анализа собеседника — нужно минимум "
#             f"{DEEP_ANALYSIS_MIN_MSGS} сообщений с обеих сторон (JSON-экспорт или "
#             "накопление через Автоматизацию чатов)."
#         )
#         return

#     # Rich Message (таблица + сворачиваемое обоснование) — основной путь;
#     # ЛЮБОЙ сбой (парсинг текста, отказ Bot API, нет капабилити у клиента и
#     # т.п.) откатывается на обычный текст, чтобы пользователь в любом случае
#     # получил результат — это платная core-фича, тишины быть не должно.
#     parsed = _parse_compat_text(data["compatibility_text"])
#     sent_rich = False
#     if parsed is not None:
#         medal_line, axes, advice = parsed
#         try:
#             rich_html = _build_rich_analysis_html(name, medal_line, axes, advice)
#             await bot.send_rich_message(
#                 chat_id=target.chat.id,
#                 rich_message=InputRichMessage(html=rich_html),
#                 reply_markup=deep_analysis_result_kb(contact_id),
#             )
#             sent_rich = True
#         except Exception:
#             logging.exception("deep_analysis: Rich Message не отправился, откат на текст")

#     if not sent_rich:
#         await _answer_long(
#             target, _format_deep_analysis(name, data), reply_markup=deep_analysis_result_kb(contact_id),
#         )


# async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
#     """Ленивая генерация с кэшем в deep_analysis, инвалидация по REBUILD_THRESHOLD
#     (тот же паттерн, что my_style_per_contact) — не пересчитываем на каждый
#     запрос. None — данных мало. Метрики (compatibility_metrics.py) считаются
#     по ВСЕЙ истории контакта без семплирования — это дёшево (текст+дата+
#     направление, без LLM), в отличие от старой системы, которой нужна была
#     урезанная выборка под лимит промпта."""
#     rows = get_all_dated_messages(owner_user_id, contact_id)
#     my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
#     ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
#     if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
#         return None

#     total_count = count_biz_messages_for_contact(owner_user_id, contact_id) + count_imported_messages(contact_id)
#     cached = get_deep_analysis(contact_id)
#     if cached and total_count - cached["last_rebuild_count"] < REBUILD_THRESHOLD:
#         return cached

#     metrics = compute_compat_metrics(rows)
#     interpretations, advice = await build_compatibility_interpretation(
#         metrics, user_gender=get_gender(owner_user_id),
#     )
#     for key, text in interpretations.items():
#         metrics[key]["interpretation"] = text

#     metrics_json = json.dumps(metrics, ensure_ascii=False)
#     save_deep_analysis(contact_id, metrics_json, advice, total_count)
#     return {"metrics_json": metrics_json, "advice_text": advice, "last_rebuild_count": total_count}


# def _short_words(text: str, max_words: int = 5) -> str:
#     """3-5 слов сути для ячейки таблицы — из уже готовой интерпретации/факта,
#     без отдельного LLM-вызова на короткую версию."""
#     text = (text or "").strip()
#     if not text:
#         return "—"
#     words = text.split()
#     short = " ".join(words[:max_words]).rstrip(".,;:")
#     if len(words) > max_words:
#         short += "…"
#     return short


# def _build_rich_analysis_html(name: str, metrics: dict, advice: str) -> str:
#     """HTML для sendRichMessage (Bot API 10.1+, aiogram InputRichMessage.html):
#     таблица с 6 метриками сразу видна, полные интерпретации — в <details> без
#     open (свёрнуто по умолчанию), совет — <mark> акцентом. metrics — dict в
#     порядке compatibility_metrics.METRICS, каждое значение {"label","short",
#     "fact","interpretation"}."""
#     esc = html.escape
#     rows = "\n".join(
#         f'<tr><td align="left">{esc(m["label"])}</td>'
#         f'<td align="center">{esc(m["short"])}</td>'
#         f'<td align="left">{esc(_short_words(m.get("interpretation") or m["fact"]))}</td></tr>'
#         for m in metrics.values()
#     )
#     detail_paras = "\n".join(
#         f"<p><b>{esc(m['label'])}</b>: {esc(m.get('interpretation') or m['fact'])}</p>"
#         for m in metrics.values()
#     )
#     return (
#         f"<h2>🔬 Анализ собеседника — {esc(name)}</h2>\n"
#         "<table>\n"
#         '<tr><th align="left">Метрика</th><th align="center">Значение</th>'
#         '<th align="left">Суть</th></tr>\n'
#         f"{rows}\n"
#         "</table>\n"
#         "<details>\n"
#         "<summary>Показать подробности</summary>\n"
#         f"{detail_paras}\n"
#         "</details>\n"
#         f"<p><mark>👉 {esc(advice)}</mark></p>"
#     )


# def _format_deep_analysis_text(name: str, metrics: dict, advice: str) -> str:
#     """Plain-text ФОЛБЭК для _run_deep_analysis, если Rich Message не
#     отправился — таблица моноширинным блоком, подробности обычным текстом,
#     без сворачивания (в чистом тексте сворачивать нечем)."""
#     header = f"🔬 Анализ собеседника — {html.escape(name)}\n\n"
#     table_lines = [f"{m['label']}: {m['short']}" for m in metrics.values()]
#     table = "<pre>" + html.escape("\n".join(table_lines)) + "</pre>"
#     details = "\n\n".join(
#         f"<b>{html.escape(m['label'])}</b>: {html.escape(m.get('interpretation') or m['fact'])}"
#         for m in metrics.values()
#     )
#     return f"{header}{table}\n\n{details}\n\n👉 {html.escape(advice)}"


def _volume_trend_to_dict(vt) -> dict:
    """VolumeTrend (compatibility_metrics.py) → JSON-совместимый dict —
    dataclass с dataclass-полями напрямую не сериализуется в json.dumps.
    is_complete/days_elapsed/calendar_days — нужны _trend_table_rows, чтобы
    пометить в таблице период, который на момент подсчёта ещё не завершён
    (см. compatibility_metrics.Period)."""
    def _period(p):
        if p is None:
            return None
        return {
            "label": p.label, "n_author": p.n_author, "n_contact": p.n_contact,
            "is_complete": p.is_complete, "days_elapsed": p.days_elapsed,
            "calendar_days": p.calendar_days,
        }
    return {
        "granularity": vt.granularity,
        "periods": [_period(p) for p in vt.periods],
        "peak": _period(vt.peak),
        "latest": _period(vt.latest),
    }


_RECENT_LINE_MAX_CHARS = 300    # одно сообщение в хвосте для LLM
_RECENT_TOTAL_MAX_CHARS = 3000  # весь хвост — на больших контактах промпт не раздувается


def _recent_dialogue_lines(rows: list[dict], limit: int = 20) -> list[str]:
    """Последние limit сообщений (дата+время+автор), хронологически — контекст
    ТОЛЬКО для блока «Готовое сообщение» в build_compatibility_interpretation,
    чтобы оно продолжало реальную последнюю тему, а не звучало общо. В
    метриках (compatibility_metrics.py) не участвует — те считаются по всей
    истории контакта отдельно."""
    texted = [r for r in rows if r.get("text") and r.get("date")]
    tail = sorted(texted, key=lambda r: r["date"])[-limit:]
    lines = []
    for r in tail:
        who = "Я" if r["direction"] == "out" else "Собеседник"
        when = r["date"][:16].replace("T", " ")
        text = r["text"] if len(r["text"]) <= _RECENT_LINE_MAX_CHARS else r["text"][:_RECENT_LINE_MAX_CHARS] + "…"
        lines.append(f"{when} {who}: {text}")
    # Было: без лимитов — одно огромное сообщение раздувало промпт. Обрезаем
    # с начала (самое свежее важнее), пока хвост не влезет в лимит.
    while lines and sum(len(l) for l in lines) > _RECENT_TOTAL_MAX_CHARS:
        lines.pop(0)
    return lines


async def _gen_deep_analysis(contact_id: int, owner_user_id: str) -> dict | None:
    """Ленивая генерация с кэшем в deep_analysis, инвалидация по REBUILD_THRESHOLD
    (тот же паттерн, что my_style_per_contact) — не пересчитываем на каждый
    запрос. None — данных мало. Метрики (compatibility_metrics.py) считаются
    по ВСЕЙ истории контакта без семплирования — дёшево (текст+дата+
    направление, без LLM), в отличие от старой 5-осевой системы, которой
    нужна была урезанная выборка под лимит промпта."""
    rows = get_all_dated_messages(owner_user_id, contact_id)
    my_count = sum(1 for r in rows if r["direction"] == "out" and r["text"])
    ct_count = sum(1 for r in rows if r["direction"] == "in" and r["text"])
    if my_count < DEEP_ANALYSIS_MIN_MSGS or ct_count < DEEP_ANALYSIS_MIN_MSGS:
        return None

    total_count = count_biz_messages_for_contact(owner_user_id, contact_id) + count_imported_messages(contact_id)
    cached = get_deep_analysis(contact_id)
    if cached and total_count - cached["last_rebuild_count"] < REBUILD_THRESHOLD:
        return cached

    # Разведочный LLM-проход для секции «Тепло» (проверка неоднозначных похвал)
    # убран вместе с самой секцией — все метрики карточки снова считаются
    # детерминированно, один вызов compute_all без предварительных обращений
    # к LLM (сам разбор метрик ниже — build_compatibility_interpretation — на
    # месте, он интерпретирует уже посчитанные числа).
    logging.info("deep_analysis: contact=%s, сообщений=%d (business + JSON)", contact_id, len(rows))
    metrics = compute_compat_metrics(rows)
    volume_trend = metrics.pop("_volume_trend")
    recent_lines = _recent_dialogue_lines(rows)
    # Было: сбой LLM-интерпретации ронял весь анализ, хотя метрики уже
    # посчитаны. Теперь — показываем метрики и факты без выводов
    # (degraded), в кэш не пишем (в следующий раз попробуем снова), попытку
    # не списываем (см. _run_deep_analysis).
    degraded = False
    try:
        interpretations, dynamics_text, synthesis, advice, ready_message = await build_compatibility_interpretation(
            metrics, volume_trend, recent_lines, user_gender=get_gender(owner_user_id),
        )
    except Exception as e:
        logging.warning("deep_analysis: интерпретация не собралась (%s: %s), contact=%s — показываю метрики без выводов",
                        type(e).__name__, str(e)[:120], contact_id)
        degraded = True
        interpretations, dynamics_text, ready_message = {}, "", ""
        synthesis = ("Выводы словами сейчас не собрались — выше всё, что посчитано по вашей переписке. "
                     "Нажми «🔄 Обновить анализ» через пару минут, допишу выводы и советы.")
        advice = ""
    for key, text in interpretations.items():
        metrics[key]["interpretation"] = text

    texted = [r for r in rows if r.get("text") and r.get("date")]
    dates = sorted(r["date"] for r in texted)
    metrics["_meta"] = {
        "total": len(texted),
        "date_from": dates[0][:10] if dates else "",
        "date_to": dates[-1][:10] if dates else "",
    }
    metrics["_volume_trend"] = _volume_trend_to_dict(volume_trend)

    metrics_json = json.dumps(metrics, ensure_ascii=False)
    if not degraded:
        save_deep_analysis(contact_id, metrics_json, dynamics_text, synthesis, advice, ready_message, total_count)
    return {
        "metrics_json": metrics_json, "dynamics_text": dynamics_text,
        "synthesis_text": synthesis, "advice_text": advice,
        "message_text": ready_message, "last_rebuild_count": total_count,
        "degraded": degraded,
    }


def deep_analysis_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить анализ", callback_data=f"deepan_refresh:{contact_id}")
    return _with_back_to_menu(b.as_markup())


def deep_analysis_free_trial_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    """Тот же полный результат, что deep_analysis_result_kb, но это
    единственная бесплатная попытка юзера (см. _run_deep_analysis) — под
    результатом отдельная строка-CTA к подписке, помимо «Обновить»/«Назад»."""
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить анализ", callback_data=f"deepan_refresh:{contact_id}")
    b.button(text="Хочешь больше разборов? → 👑 Подписка", callback_data="show_premium")
    b.adjust(1)
    return _with_back_to_menu(b.as_markup())


def deep_analysis_teaser_kb(contact_id: int) -> InlineKeyboardMarkup:
    """«Видимый» пейволл после исчерпания бесплатной попытки (см.
    _format_deep_analysis_teaser) — вместо текстовой заглушки."""
    b = InlineKeyboardBuilder()
    b.button(text="👑 Разблокировать полный анализ", callback_data="show_premium")
    b.button(text="🔄 Обновить анализ", callback_data=f"deepan_refresh:{contact_id}")
    b.adjust(1)
    return _with_back_to_menu(b.as_markup())


_DEEP_ANALYSIS_FREE_METRIC_KEYS = ("balance", "response_speed")
_DEEP_ANALYSIS_LOCKED_TEASER = (
    "🔒 Ещё 6 разборов доступны в Premium — инициатива, паузы, кто чаще "
    "спрашивает, совпадение по времени, динамика переписки, персональные "
    "рекомендации."
)


def _format_deep_analysis_teaser(name: str, metrics: dict) -> str:
    """Урезанная карточка для юзера без Premium с уже потраченной бесплатной
    попыткой — первые 2 метрики (_DEEP_ANALYSIS_FREE_METRIC_KEYS) полностью,
    остальные секции заменены одним тизер-блоком. Метрики и интерпретации
    посчитаны ПОЛНОСТЬЮ как обычно (см. _gen_deep_analysis, тот же кэш, что
    у Premium-юзеров) — здесь только решаем, что показать."""
    meta = metrics.get("_meta") or {}
    header = f"🔬 Анализ собеседника — {html.escape(name)}\n"
    if meta.get("total"):
        header += f"<i>{meta['total']} сообщений, {html.escape(meta.get('date_from', ''))} — {html.escape(meta.get('date_to', ''))}</i>\n"
    header += "\n"

    parts = []
    for key in _DEEP_ANALYSIS_FREE_METRIC_KEYS:
        m = metrics.get(key)
        if not m:
            continue
        text = m.get("interpretation") or m["fact"]
        text += _quote_examples_suffix(m)
        parts.append(f"<b>{_METRIC_EMOJI.get(key, '')} {html.escape(m['label'])}</b>\n{html.escape(text)}")
    metric_parts = "\n\n".join(parts)

    return f"{header}{metric_parts}\n\n{_DEEP_ANALYSIS_LOCKED_TEASER}"


# async def _run_deep_analysis(
#     bot: Bot, target: Message, telegram_id: str, contact_id: int, edit: bool = False
# ) -> None:
#     # Реферальная награда теперь даёт полный Premium (учтено внутри _is_premium,
#     # которую вызывает _require_premium) — отдельной проверки тут больше не нужно.
#     if not await _require_premium(bot, target, telegram_id):
#         return
#     contact = get_contact_by_id(contact_id)
#     if not contact:
#         text = "Контакт не найден."
#         await (target.edit_text(text) if edit else target.answer(text))
#         return
#     name = _contact_name(contact)

#     wait_text = f"Готовлю анализ собеседника — {name}. Это займёт ~20 секунд..."
#     await (target.edit_text(wait_text) if edit else target.answer(wait_text))

#     try:
#         data = await _gen_deep_analysis(contact_id, telegram_id)
#     except RateLimitError:
#         await target.answer("Лимит LLM исчерпан, попробуй позже.")
#         return
#     except Exception:
#         logging.exception("deep_analysis: ошибка генерации")
#         await target.answer("Не удалось сгенерировать анализ — попробуй ещё раз.")
#         return

#     if not data:
#         await target.answer(
#             f"Пока маловато данных по {name} для анализа собеседника — нужно минимум "
#             f"{DEEP_ANALYSIS_MIN_MSGS} сообщений с обеих сторон (JSON-экспорт или "
#             "накопление через Автоматизацию чатов)."
#         )
#         return

#     metrics = json.loads(data["metrics_json"])
#     advice = data["advice_text"]

#     # Rich Message (таблица + сворачиваемые подробности) — основной путь;
#     # ЛЮБОЙ сбой (отказ Bot API, нет капабилити у клиента и т.п.) откатывается
#     # на обычный текст, чтобы пользователь в любом случае получил результат —
#     # это платная core-фича, тишины быть не должно.
#     sent_rich = False
#     try:
#         rich_html = _build_rich_analysis_html(name, metrics, advice)
#         await bot.send_rich_message(
#             chat_id=target.chat.id,
#             rich_message=InputRichMessage(html=rich_html),
#             reply_markup=deep_analysis_result_kb(contact_id),
#         )
#         sent_rich = True
#     except Exception:
#         logging.exception("deep_analysis: Rich Message не отправился, откат на текст")

#     if not sent_rich:
#         await _answer_long(
#             target, _format_deep_analysis_text(name, metrics, advice),
#             reply_markup=deep_analysis_result_kb(contact_id), parse_mode="HTML",
#         )


_METRIC_EMOJI = {
    "balance": "⚖️",
    "response_speed": "⏱️",
    "initiation": "🙋",
    "long_pauses": "⏸️",
    "questions": "❓",
    "circadian": "🕒",
}

def _direction_label(direction: str) -> str:
    return "ты" if direction == "out" else "собеседник"


def _quote_examples_suffix(m: dict) -> str:
    """Доп. строка с реальными цитатами для любой метрики, у которой они есть
    (поле "examples" — сейчас его отдаёт «Кто чаще задаёт вопросы», см.
    compatibility_metrics.question_balance). У метрик без цитат поля нет и
    суффикс пустой, поэтому вызывать можно на всех подряд, без частных
    условий по ключу. Цитаты обрезаются до 80 символов — тот же паттерн, что
    у initiative_axis в features.py. Добавляется ПОСЛЕ interpretation/fact, а
    не встраивается в промпт интерпретации: LLM переписывает только смысл
    факта и не видит сырые цитаты, значит не может их исказить/потерять."""
    examples = m.get("examples") or []
    if not examples:
        return ""

    def _quote(text: str) -> str:
        return text if len(text) <= 80 else text[:77].rstrip() + "…"

    parts = [
        f"«{_quote(text)}» ({_direction_label(direction)})"
        for direction, text in examples
    ]
    return " Например, " + "; ".join(parts) + "."


_MAX_TREND_ROWS = 12  # длинная история (месяцы) может дать 30+ периодов — не годится для одной таблицы в сообщении


def _trend_table_rows(vt: dict) -> list[dict]:
    """Последние _MAX_TREND_ROWS периодов для таблицы — полная история всё
    равно участвует в подсчёте peak/latest (см. compatibility_metrics.py),
    здесь только отображение."""
    periods = vt.get("periods") or []
    return periods[-_MAX_TREND_ROWS:]


def _trend_row_label(p: dict) -> str:
    """Подпись периода для таблицы — с явной пометкой, если период ещё не
    завершён («09.2026 (5 дней из 30, ещё не завершён)»), чтобы читатель не
    принял сырую сумму незавершённого периода за законченный показатель
    (сама сумма в таблице не меняется — только подпись)."""
    if p.get("is_complete", True):
        return p["label"]
    return f"{p['label']} ({p['days_elapsed']} дней из {p['calendar_days']}, ещё не завершён)"


def _rich_heading(text: str) -> str:
    """Жирный заголовок внутри Rich Message: <p><b>...</b></p>, НЕ
    <h2>/<h3> (heading-блок, RichBlockSectionHeading в Bot API 10.1+).
    Heading-блок — отдельный тип блока со своим "size" (1-6), и каждый
    Telegram-клиент вправе рисовать его собственной типографикой — на
    iPhone это оказался крупный засечный шрифт, на десктопе/вебе — обычный
    текст. Bold-текст ВНУТРИ параграфа (RichBlockParagraph) такому не
    подвержен — это тот же механизм, что уже даёт одинаковый на всех
    платформах blockquote (тоже просто paragraph-контент, не отдельный
    блок-тип). Единая точка форматирования жирных заголовков во всей
    карточке — и для общего заголовка, и для заголовков метрик/секций, не
    два разных места."""
    return f"<p><b>{html.escape(text)}</b></p>"


def _ready_message_block_html(name: str, message_text: str) -> str:
    """Моноширинный блок «Напиши сейчас, {name}!» внизу карточки анализа —
    готовое сообщение, которое можно скопировать и отправить прямо сейчас
    (build_compatibility_interpretation в llm.py). Возвращена по фидбеку:
    была отдельным tap-to-copy сообщением до 6d58335, теперь — блок в конце
    той же карточки. Пусто, если LLM не вернула текст (не выдумываем)."""
    if not message_text.strip():
        return ""
    return (
        f"{_rich_heading(f'✉️ Напиши сейчас, {name}!')}\n"
        f"<pre>{html.escape(message_text.strip())}</pre>\n"
    )


def _build_rich_analysis_html(name: str, metrics: dict, dynamics_text: str, synthesis: str, advice: str, message_text: str) -> str:
    """HTML для sendRichMessage (Bot API 10.1+, aiogram InputRichMessage.html):
    жирный заголовок (см. _rich_heading — НЕ <h2>/<h3>) с эмодзи по смыслу
    метрики + <blockquote> под ним на каждую метрику, отдельная секция
    «Динамика переписки» с настоящей таблицей периодов, «Вывод» — синтез,
    «Что дальше» — совет обычным текстом, без выделения, «Напиши сейчас» —
    готовое сообщение моноширинным блоком (см. _ready_message_block_html)."""
    esc = html.escape
    meta = metrics.get("_meta") or {}
    vt = metrics.get("_volume_trend") or {}

    subtitle = ""
    if meta.get("total"):
        subtitle = (
            f"<p><i>{meta['total']} сообщений, {esc(meta.get('date_from', ''))} — "
            f"{esc(meta.get('date_to', ''))}</i></p>\n"
        )

    metric_block_parts = []
    for key, m in metrics.items():
        if key.startswith("_"):
            continue
        text = m.get("interpretation") or m["fact"]
        text += _quote_examples_suffix(m)  # пусто у метрик без цитат
        heading = f"{_METRIC_EMOJI.get(key, '')} {m['label']}".strip()
        metric_block_parts.append(
            f"{_rich_heading(heading)}\n<blockquote>{esc(text)}</blockquote>"
        )
    metric_blocks = "\n".join(metric_block_parts)

    trend_rows = _trend_table_rows(vt)
    if trend_rows:
        table_lines = "\n".join(
            f'<tr><td align="left">{esc(_trend_row_label(p))}</td>'
            f'<td align="center">{p["n_author"]}</td>'
            f'<td align="center">{p["n_contact"]}</td>'
            f'<td align="center">{p["n_author"] + p["n_contact"]}</td></tr>'
            for p in trend_rows
        )
        trend_table = (
            "<table>\n"
            '<tr><th align="left">Период</th><th align="center">Ты</th>'
            '<th align="center">Собеседник</th><th align="center">Всего</th></tr>\n'
            f"{table_lines}\n"
            "</table>\n"
        )
    else:
        trend_table = ""

    return (
        f"{_rich_heading(f'🔬 Анализ собеседника — {name}')}\n"
        f"{subtitle}"
        f"{metric_blocks}\n"
        f"{_rich_heading('📈 Динамика переписки')}\n"
        f"{trend_table}"
        f"<blockquote>{esc(dynamics_text)}</blockquote>\n"
        f"{_rich_heading('🧩 Вывод')}\n"
        f"<blockquote>{esc(synthesis)}</blockquote>\n"
        f"{_rich_heading('👉 Что дальше')}\n"
        f"<p>{esc(advice)}</p>\n"
        f"{_ready_message_block_html(name, message_text)}"
    )


def _format_deep_analysis_text(name: str, metrics: dict, dynamics_text: str, synthesis: str, advice: str, message_text: str) -> str:
    """Plain-text ФОЛБЭК для _run_deep_analysis, если Rich Message не
    отправился — та же структура, эмодзи-заголовки вместо heading, таблица
    периодов моноширинным блоком. HTML-спецсимволы в значениях (например
    «<1 мин») экранируются ДО оборачивания в <pre>/<b> — иначе parse_mode=
    HTML сочтёт их невалидными тегами и упадёт сам фолбэк."""
    meta = metrics.get("_meta") or {}
    vt = metrics.get("_volume_trend") or {}

    header = f"🔬 Анализ собеседника — {html.escape(name)}\n"
    if meta.get("total"):
        header += f"<i>{meta['total']} сообщений, {html.escape(meta.get('date_from', ''))} — {html.escape(meta.get('date_to', ''))}</i>\n"
    header += "\n"

    metric_part_list = []
    for key, m in metrics.items():
        if key.startswith("_"):
            continue
        text = m.get("interpretation") or m["fact"]
        text += _quote_examples_suffix(m)  # пусто у метрик без цитат
        metric_part_list.append(
            f"<b>{_METRIC_EMOJI.get(key, '')} {html.escape(m['label'])}</b>\n{html.escape(text)}"
        )
    metric_parts = "\n\n".join(metric_part_list)

    trend_rows = _trend_table_rows(vt)
    if trend_rows:
        lines = [f"{_trend_row_label(p)}: ты {p['n_author']}, собеседник {p['n_contact']}, всего {p['n_author'] + p['n_contact']}" for p in trend_rows]
        trend_table = "<pre>" + html.escape("\n".join(lines)) + "</pre>\n"
    else:
        trend_table = ""

    ready_part = ""
    if message_text.strip():
        ready_part = (
            f"\n\n✉️ <b>Напиши сейчас, {html.escape(name)}!</b>\n"
            f"<pre>{html.escape(message_text.strip())}</pre>"
        )

    return (
        f"{header}{metric_parts}\n\n"
        f"📈 <b>Динамика переписки</b>\n{trend_table}{html.escape(dynamics_text)}\n\n"
        f"🧩 <b>Вывод</b>\n{html.escape(synthesis)}\n\n"
        f"👉 <b>Что дальше</b>\n{html.escape(advice)}"
        f"{ready_part}"
    )


async def _run_deep_analysis(
    bot: Bot, target: Message, telegram_id: str, contact_id: int, edit: bool = False
) -> None:
    # «Видимый» пейволл вместо блокировки на входе: карточка считается
    # ПОЛНОСТЬЮ в любом случае (тот же кэш, что у Premium — не дороже), а
    # решение показать всё/урезанно принимается только на этапе рендера
    # ниже (full_access). is_free_trial_result — эта генерация и есть
    # единственная бесплатная попытка юзера (ещё не потрачена, не Premium) —
    # только в этом случае под полным результатом добавляется CTA-строка к
    # подписке (deep_analysis_free_trial_result_kb), см. п.2 конверсии.
    is_premium = await _is_premium(bot, telegram_id)
    analysis_used = get_analysis_trial_count(telegram_id)
    # Базовой бесплатной попытки больше нет (было "1 +") — только бонус.
    analysis_limit = get_analysis_trial_bonus(telegram_id)
    full_access = is_premium or analysis_used < analysis_limit
    is_free_trial_result = full_access and not is_premium

    contact = get_contact_by_id(contact_id)
    if not contact:
        text = "Контакт не найден."
        await (target.edit_text(text) if edit else target.answer(text))
        _note_outcome("stale")
        return
    name = _contact_name(contact)
    retry_cb = f"deepan:{contact_id}"

    # _gen_deep_analysis сам решает кэш-хит внутри — обёртка анимации просто
    # оборачивает весь вызов: на кэш-хите анимация мелькнёт одним кадром и
    # тут же отменится, реальный вызов LLM крутит её честно.
    progress_msg = await (_edit_or_keep(target, _PROGRESS_FRAMES[0]) if edit else target.answer(_PROGRESS_FRAMES[0]))
    try:
        data = await with_progress_animation(
            bot, progress_msg.chat.id, progress_msg.message_id,
            _gen_deep_analysis(contact_id, telegram_id),
        )
    except RateLimitError:
        _deep_analysis_failed(telegram_id, contact_id, "rate_limit")
        await progress_msg.edit_text("Лимит LLM исчерпан, попробуй позже.", reply_markup=_retry_kb(retry_cb))
        _note_outcome("rate_limit")
        return
    except Exception as e:
        # Было: одна общая заглушка для всего. Теперь причина — в журнал и в
        # событие gen_deep_analysis_failed; LLM-сбой сюда больше не доходит
        # (метрики показываются без выводов, см. _gen_deep_analysis), значит
        # здесь — ошибка в данных/расчёте.
        logging.exception("deep_analysis: ошибка расчёта, contact=%s owner=%s", contact_id, telegram_id)
        _deep_analysis_failed(telegram_id, contact_id, f"data_error:{type(e).__name__}")
        await progress_msg.edit_text("Не получилось собрать анализ, попробуй ещё раз.", reply_markup=_retry_kb(retry_cb))
        _note_outcome("llm_error")
        return

    if not data:
        await progress_msg.edit_text(
            f"Пока маловато данных по {name} для анализа собеседника — нужно минимум "
            f"{DEEP_ANALYSIS_MIN_MSGS} сообщений с обеих сторон (JSON-экспорт или "
            "накопление через Автоматизацию чатов).",
            reply_markup=_with_back_to_menu(InlineKeyboardMarkup(inline_keyboard=[])),
        )
        _note_outcome("no_data")
        return

    metrics = json.loads(data["metrics_json"])
    dynamics_text = data["dynamics_text"]
    synthesis = data["synthesis_text"]
    advice = data["advice_text"]
    message_text = data["message_text"]

    if not full_access and _never_unlocked(telegram_id, is_premium):
        # Пул ещё ни разу не открывался — вместо тизера экран «подпишись на
        # канал»; анализ уже посчитан и лежит в кэше, «👀 Показать» после
        # подписки достанет его без нового вызова LLM.
        await progress_msg.edit_text(
            _unlock_gate_text("analysis"), reply_markup=_unlock_gate_kb("analysis", contact_id),
        )
        await _record_gate_event(telegram_id, "gate_shown", "analysis")
        _note_outcome("no_quota")
        return

    if not full_access:
        # Пробник уже потрачен, Premium нет — «видимый» пейволл: первые 2
        # секции полностью + тизер вместо остальных, а не текстовая
        # заглушка. Всегда простым текстом (не Rich Message) — сюда ведёт
        # кнопка «👑 Разблокировать», а cb_show_premium редактирует ЭТО ЖЕ
        # сообщение (edit_text), что для Rich Message не гарантировано.
        await _edit_or_answer_long(
            progress_msg, _format_deep_analysis_teaser(name, metrics),
            reply_markup=deep_analysis_teaser_kb(contact_id), parse_mode="HTML",
        )
        _note_outcome("no_quota")
        return

    result_kb = (
        deep_analysis_free_trial_result_kb(contact_id) if is_free_trial_result
        else deep_analysis_result_kb(contact_id)
    )

    # Rich Message (заголовки + цитаты на метрику + таблица динамики) —
    # основной путь; ЛЮБОЙ сбой (отказ Bot API, нет капабилити у клиента и
    # т.п.) откатывается на обычный текст, чтобы пользователь в любом случае
    # получил результат — это платная core-фича, тишины быть не должно.
    # Rich Message — ДРУГОЙ тип контента в Bot API, его нельзя получить
    # редактированием обычного текстового сообщения (edit_message_text), т.е.
    # progress_msg физически не может «превратиться» в него — поэтому в этой
    # ветке прогресс-сообщение гасится (удаляется), а результат уходит НОВЫМ
    # sendRichMessage. Текстовый фолбэк ниже, наоборот, продолжает ту же
    # анимацию тем же edit_message_text, как и остальные два флоу.
    sent_rich = False
    try:
        rich_html = _build_rich_analysis_html(name, metrics, dynamics_text, synthesis, advice, message_text)
        await bot.send_rich_message(
            chat_id=progress_msg.chat.id,
            rich_message=InputRichMessage(html=rich_html),
            reply_markup=result_kb,
        )
        sent_rich = True
    except Exception:
        logging.exception("deep_analysis: Rich Message не отправился, откат на текст")

    if sent_rich:
        try:
            await progress_msg.delete()
        except Exception:
            pass  # не критично — прогресс-сообщение просто останется висеть над результатом
    else:
        await _edit_or_answer_long(
            progress_msg, _format_deep_analysis_text(name, metrics, dynamics_text, synthesis, advice, message_text),
            reply_markup=result_kb, parse_mode="HTML",
        )

    if data.get("degraded"):
        # Показали только метрики без выводов — попытку не списываем.
        _deep_analysis_failed(telegram_id, contact_id, "degraded")
    else:
        await _charge_feature_trial_if_needed(bot, telegram_id, mark_analysis_trial_used)
    _note_outcome("ok")
    try:
        record_event(telegram_id, "gen_deep_analysis", str(contact_id))
    except Exception:
        logging.exception("telemetry: не удалось записать событие анализа")


def _deep_analysis_failed(telegram_id: str, contact_id: int, reason: str) -> None:
    """Событие gen_deep_analysis_failed (meta "<причина>:<contact_id>"):
    rate_limit / degraded (выводы LLM не собрались, показаны метрики) /
    data_error:<тип исключения>."""
    try:
        record_event(telegram_id, "gen_deep_analysis_failed", f"{reason}:{contact_id}")
    except Exception:
        logging.exception("telemetry: не удалось записать gen_deep_analysis_failed")


async def _show_deep_analysis(
    message: Message, bot: Bot, telegram_id: str | None = None, edit: bool = False,
) -> None:
    # telegram_id передаётся явно из cb_submenu/cb_main_menu_action
    # (call.from_user), т.к. message там — это сообщение БОТА с инлайн-
    # клавиатурой, а не сообщение юзера, и message.from_user в этом случае
    # был бы ботом, а не человеком. edit=True — оттуда же (главное меню):
    # пейволл/статус редактируют то же сообщение, а не шлют новое (финальный
    # результат — всегда отдельное новое сообщение, см. _run_deep_analysis).
    telegram_id = telegram_id or str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await handlers.start_menu._send_no_contacts_hint(message)
        return

    if len(contacts) == 1:
        await _run_deep_analysis(bot, message, telegram_id, contacts[0]["id"], edit=edit)
        return

    if edit:
        await message.edit_text("Для кого сделать анализ собеседника?", reply_markup=contacts_kb(contacts, "deepan"))
    else:
        await message.answer("Для кого сделать анализ собеседника?", reply_markup=contacts_kb(contacts, "deepan"))


@dp.message(Command("deep_analysis"))
async def cmd_deep_analysis(message: Message, bot: Bot) -> None:
    await _show_deep_analysis(message, bot)


@dp.callback_query(F.data.startswith("deepan_refresh:"))
async def cb_deep_analysis_refresh(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer("Пересобираю анализ...")
    delete_deep_analysis(contact_id)
    await _run_deep_analysis(bot, call.message, telegram_id, contact_id)


@dp.callback_query(F.data.startswith("deepan:"))
async def cb_deep_analysis_contact(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer()
    await _run_deep_analysis(bot, call.message, telegram_id, contact_id, edit=True)


# ── 💐 Идеальное свидание ─────────────────────────────────────────────────────

IDEAL_DATE_MIN_MSGS = 5  # минимум сообщений собеседника, иначе не за что зацепиться


def _spread_sample(rows: list[dict], direction: str, target: int, offset: float = 0.0) -> list[str]:
    """Равномерная выборка target сообщений заданного направления по ВСЕЙ истории
    (не только последние N) — как периодизация в _periodized_dated_lines, но
    плоским списком текстов. Так упоминания интересов из любого периода переписки
    попадают в промпт, а не только из свежих сообщений.
    offset ∈ [0,1) сдвигает точку внутри каждого временного окна — при offset=0
    выборка детерминированная, со случайным offset «Другая идея» видит ДРУГИЕ
    сообщения (тот же равномерный охват, другие представители)."""
    msgs = [
        r["text"] for r in sorted(
            (r for r in rows if r["direction"] == direction and r["text"] and r["text"].strip()),
            key=lambda r: r["date"],
        )
    ]
    if len(msgs) <= target:
        return msgs
    step = len(msgs) / target
    last = len(msgs) - 1
    return [msgs[min(last, int(i * step + offset * step))] for i in range(target)]


def _ideal_date_samples(contact_id: int, owner_user_id: str, offset: float = 0.0) -> dict | None:
    """Семплы для build_ideal_date по ВСЕЙ истории переписки (business + JSON,
    через get_all_dated_messages) с равномерным охватом всех периодов — как в
    «Анализе собеседника», а не только последние сообщения. offset сдвигает
    выборку («Другая идея» → другие сообщения). None — сообщений собеседника
    слишком мало для осмысленной идеи."""
    rows = get_all_dated_messages(owner_user_id, contact_id)
    contact_msgs = _spread_sample(rows, "in", 100, offset)
    my_msgs      = _spread_sample(rows, "out", 40, offset)
    if len(contact_msgs) < IDEAL_DATE_MIN_MSGS:
        return None
    stats = _deep_stats_summary(rows)
    return {"contact_sample": contact_msgs, "my_sample": my_msgs, "features_summary": stats}


async def _gen_ideal_date(contact_id: int, owner_user_id: str, fresh: bool = False) -> dict | None:
    """Ленивая генерация с кэшем в ideal_date. None — данных мало.
    fresh=True («Другая идея») — не читает кэш и берёт СЛУЧАЙНО сдвинутую
    выборку, чтобы модель увидела другие сообщения и дала заметно другую идею."""
    if not fresh:
        cached = get_ideal_date(contact_id)
        if cached:
            return cached

    offset = random.random() if fresh else 0.0
    samples = _ideal_date_samples(contact_id, owner_user_id, offset)
    if not samples:
        return None

    interaction_card = await _gen_interaction_card(contact_id, owner_user_id) or ""
    date_idea, gift_ideas = await build_ideal_date(
        samples["contact_sample"], samples["my_sample"],
        interaction_card, samples["features_summary"],
    )
    save_ideal_date(contact_id, date_idea, gift_ideas)
    return {"date_idea": date_idea, "gift_ideas": gift_ideas}


def _format_ideal_date(name: str, data: dict) -> str:
    """Оба блока (идея свидания + подарки) — одним сообщением."""
    return (
        f"💐 Идеальное свидание — {name}\n\n"
        f"{data['date_idea'].strip()}\n\n"
        f"{data['gift_ideas'].strip()}"
    )


_IDEAL_DATE_LOCKED_TEASER = (
    "🔒 Идеи подарков доступны в Premium — конкретные варианты под этого "
    "человека, а не общие категории."
)


def _format_ideal_date_teaser(name: str, data: dict) -> str:
    """Урезанная версия для юзера без Premium с уже потраченной бесплатной
    попыткой. build_ideal_date (llm.py) генерирует РОВНО одну идею свидания
    + один блок подарков (не список вариантов на выбор) — «первая идея» из
    задачи конверсии это единственная идея свидания, она показывается
    полностью, а блок подарков заменяется тизером."""
    return (
        f"💐 Идеальное свидание — {name}\n\n"
        f"{data['date_idea'].strip()}\n\n"
        f"{_IDEAL_DATE_LOCKED_TEASER}"
    )


def ideal_date_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другая идея", callback_data=f"idealdate_refresh:{contact_id}")
    return _with_back_to_menu(b.as_markup())


def ideal_date_free_trial_result_kb(contact_id: int) -> InlineKeyboardMarkup:
    """Тот же полный результат, что ideal_date_result_kb, но это единственная
    бесплатная попытка юзера — под результатом отдельная строка-CTA."""
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Другая идея", callback_data=f"idealdate_refresh:{contact_id}")
    b.button(text="Хочешь больше идей? → 👑 Подписка", callback_data="show_premium")
    b.adjust(1)
    return _with_back_to_menu(b.as_markup())


def ideal_date_teaser_kb(contact_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👑 Разблокировать подарки", callback_data="show_premium")
    b.button(text="🔄 Другая идея", callback_data=f"idealdate_refresh:{contact_id}")
    b.adjust(1)
    return _with_back_to_menu(b.as_markup())


async def _run_ideal_date(
    bot: Bot, target: Message, telegram_id: str, contact_id: int,
    edit: bool = False, fresh: bool = False,
) -> None:
    # «Видимый» пейволл вместо блокировки на входе — см. коммент у
    # _run_deep_analysis, та же схема: full_access решает рендер, не вход.
    is_premium = await _is_premium(bot, telegram_id)
    date_used = get_date_trial_count(telegram_id)
    # Базовой бесплатной попытки больше нет (было "1 +") — только бонус.
    date_limit = get_date_trial_bonus(telegram_id)
    full_access = is_premium or date_used < date_limit
    is_free_trial_result = full_access and not is_premium

    contact = get_contact_by_id(contact_id)
    if not contact:
        text = "Контакт не найден."
        await (target.edit_text(text) if edit else target.answer(text))
        _note_outcome("stale")
        return
    name = _contact_name(contact)
    retry_cb = f"idealdate:{contact_id}"

    # _gen_ideal_date сам решает кэш-хит внутри (см. deep_analysis выше) —
    # обёртка анимации просто оборачивает весь вызов.
    progress_msg = await (_edit_or_keep(target, _PROGRESS_FRAMES[0]) if edit else target.answer(_PROGRESS_FRAMES[0]))
    try:
        data = await with_progress_animation(
            bot, progress_msg.chat.id, progress_msg.message_id,
            _gen_ideal_date(contact_id, telegram_id, fresh=fresh),
        )
    except RateLimitError:
        await progress_msg.edit_text("Лимит LLM исчерпан, попробуй позже.", reply_markup=_retry_kb(retry_cb))
        _note_outcome("rate_limit")
        return
    except Exception:
        logging.exception("ideal_date: ошибка генерации")
        await progress_msg.edit_text("Не получилось собрать идею, попробуй ещё раз.", reply_markup=_retry_kb(retry_cb))
        _note_outcome("llm_error")
        return

    if not data:
        await progress_msg.edit_text(
            f"Пока маловато сообщений от {name}, чтобы зацепиться за что-то "
            f"конкретное — нужно хотя бы {IDEAL_DATE_MIN_MSGS} его сообщений "
            "(JSON-экспорт или накопление через Автоматизацию чатов)."
        )
        _note_outcome("no_data")
        return

    if not full_access and _never_unlocked(telegram_id, is_premium):
        # См. то же место в _run_deep_analysis — идея уже в кэше.
        await progress_msg.edit_text(
            _unlock_gate_text("date"), reply_markup=_unlock_gate_kb("date", contact_id),
        )
        await _record_gate_event(telegram_id, "gate_shown", "date")
        _note_outcome("no_quota")
        return

    if not full_access:
        await _edit_or_answer_long(
            progress_msg, _format_ideal_date_teaser(name, data),
            reply_markup=ideal_date_teaser_kb(contact_id),
        )
        _note_outcome("no_quota")
        return

    result_kb = (
        ideal_date_free_trial_result_kb(contact_id) if is_free_trial_result
        else ideal_date_result_kb(contact_id)
    )
    await _edit_or_answer_long(progress_msg, _format_ideal_date(name, data), reply_markup=result_kb)
    await _charge_feature_trial_if_needed(bot, telegram_id, mark_date_trial_used)
    _note_outcome("ok")
    try:
        record_event(telegram_id, "gen_ideal_date", str(contact_id))
    except Exception:
        logging.exception("telemetry: не удалось записать событие свидания")


async def _show_ideal_date(
    message: Message, bot: Bot, telegram_id: str | None = None, edit: bool = False,
) -> None:
    telegram_id = telegram_id or str(message.from_user.id)
    contacts = list_contacts(telegram_id)
    if not contacts:
        await handlers.start_menu._send_no_contacts_hint(message)
        return

    if len(contacts) == 1:
        await _run_ideal_date(bot, message, telegram_id, contacts[0]["id"], edit=edit)
        return

    if edit:
        await message.edit_text("С кем свидание?", reply_markup=contacts_kb(contacts, "idealdate"))
    else:
        await message.answer("С кем свидание?", reply_markup=contacts_kb(contacts, "idealdate"))


@dp.callback_query(F.data.startswith("idealdate_refresh:"))
async def cb_ideal_date_refresh(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer("Придумываю другую идею...")
    delete_ideal_date(contact_id)
    await _run_ideal_date(bot, call.message, telegram_id, contact_id, fresh=True)


@dp.callback_query(F.data.startswith("idealdate:"))
async def cb_ideal_date_contact(call: CallbackQuery, bot: Bot) -> None:
    contact_id  = int(call.data.split(":")[1])
    telegram_id = str(call.from_user.id)
    await call.answer()
    await _run_ideal_date(bot, call.message, telegram_id, contact_id, edit=True)
