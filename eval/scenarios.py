"""Банк сценариев для eval-прогона промптов генерации.
Каждый сценарий — вход + ожидаемые ограничения к ответу. Синтетика, реальные
чаты не нужны. Стадия/сигналы считаются теми же функциями, что в проде.
"""
from features import detect_reply_situation, stage_hint

STYLE_CARD = (
    "🎙️ Голос и тон\n• Коротко, с иронией, на «ты», строчными буквами\n"
    "✍️ Как строишь сообщения\n• 1-2 предложения, редко длиннее\n"
    "🧩 Словарь\n• «норм», «кстати», «слушай» — разговорный\n"
    "🔤 Регистр\n• всё с маленькой буквы, эмодзи почти нет\n"
)
INTERACTION_CARD = (
    "🎯 Как писать\n• охотнее отвечает на конкретику и лёгкий юмор\n"
    "📏 Длина\n• пишет коротко, 1-2 строки, эмодзи редко\n"
    "🔤 Регистр\n• на «ты», строчными\n"
    "🧊 Что гасит\n• длинные простыни и формальный тон\n"
)


def signals(my_total: int, c_total: int, last_incoming: str):
    parts = []
    if my_total + c_total >= 4:
        parts.append(stage_hint(my_total, c_total))
    s = detect_reply_situation(last_incoming)
    if s:
        parts.append(s)
    return "\n".join(f"• {p}" for p in parts) if parts else None


# expects — набор флагов-ограничений, проверяемых eval/checks.py:
#   no_foreign, no_ai_stock, no_begging, no_cliche_opener, max_words
def _reply(id, incoming, style, totals, tags, max_words=40):
    my, c = totals
    return {
        "id": id, "kind": "reply", "style": style,
        "incoming": incoming,
        "data_signals": signals(my, c, incoming),
        "expects": {"no_foreign": True, "no_ai_stock": True, "max_words": max_words, **tags},
    }


def _rewrite(id, draft, style, tags, max_words=45):
    return {
        "id": id, "kind": "rewrite", "style": style, "draft": draft,
        "expects": {"no_foreign": True, "no_ai_stock": True, "max_words": max_words, **tags},
    }


SCENARIOS = [
    # ── rewrite ──
    _rewrite("rw-confident", "привет, увидел что ты тоже из питера, я там вырос, соскучился по городу", "confident", {"no_cliche_opener": True}),
    _rewrite("rw-apology", "извини что пропал на пару дней, завал на работе был, но я про тебя помнил", "tender", {}),
    _rewrite("rw-passive-aggr", "ну ты так и не ответила про выходные, ладно, видимо неинтересно", "friendly", {"no_begging": True}),
    _rewrite("rw-ask-out", "слушай а может сходим куда-нибудь на выходных, кофе или прогулка", "flirt", {}),
    _rewrite("rw-rambling", "привет ну как ты вообще как дела что нового на работе я вот думал про наш разговор про путешествия куда ты хочешь съездить", "humor", {}),
    # ── reply: обычные ──
    _reply("rp-normal-flirt", "ой а я как раз мечтаю съездить в грузию", "flirt", (40, 35), {"no_cliche_opener": True}),
    _reply("rp-flirt-incoming", "ты мне сегодня снился кстати", "flirt", (50, 48), {}),
    _reply("rp-logistics", "ну что, во сколько завтра встречаемся?", "friendly", (70, 65), {}),
    _reply("rp-humor-joke", "я тут пиццу сжёг, кажется готовить я не умею совсем", "humor", (45, 40), {}),
    _reply("rp-fresh-light", "привет) прикольная у тебя собака на фото", "friendly", (3, 2), {}),
    _reply("rp-formal-early", "здравствуйте, приятно познакомиться", "formal", (2, 1), {}),
    _reply("rp-established-nudge", "ахаха ну ты даёшь, с тобой реально весело переписываться", "flirt", (140, 130), {}),
    # ── reply: тяжёлые (главные пробы) ──
    _reply("rp-dry-ok", "ок", "friendly", (90, 85), {"no_begging": True, "no_cliche_opener": True}, max_words=18),
    _reply("rp-dry-ugu", "угу", "humor", (90, 85), {"no_begging": True}, max_words=18),
    _reply("rp-rejection", "слушай давай не будем, мне сейчас не до знакомств", "confident", (15, 12), {"no_begging": True, "no_cliche_opener": True}),
    _reply("rp-cold", "да мне в общем-то всё равно", "confident", (30, 25), {"no_begging": True}),
    _reply("rp-vuln-fear", "мечтаю попробовать но мне страшно если честно", "tender", (40, 35), {}),
    _reply("rp-vuln-tired", "извини, я сегодня никакая, на работе полный ад и я вымоталась", "tender", (60, 55), {}),
    _reply("rp-hostile", "ты вообще читаешь что я пишу? бесит", "confident", (25, 20), {"no_begging": True}),
    _reply("rp-ghost-return", "прив, извини пропала на неделю, закрутилась", "friendly", (50, 45), {}),
]
