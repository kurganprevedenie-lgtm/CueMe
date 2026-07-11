"""Тесты чистых хелперов main.py (без сети/aiogram-раннера).
Импорт main поднимает Bot/Dispatcher, но без соединения — этого достаточно."""
import main


# ── _contact_name ─────────────────────────────────────────────────────────────

def test_contact_name_variants():
    assert main._contact_name({"display_name": "Аня", "username": "anya", "contact_alias": "x"}) == "Аня (@anya)"
    assert main._contact_name({"display_name": "", "username": "anya", "contact_alias": "x"}) == "@anya"
    assert main._contact_name({"display_name": "Аня", "username": "", "contact_alias": "x"}) == "Аня"
    # ни имени, ни юзернейма → алиас
    assert main._contact_name({"display_name": "", "username": "", "contact_alias": "alias-uuid"}) == "alias-uuid"


def test_contact_name_without_username_column():
    # строка без колонки username не должна падать (ветка "username" in keys)
    assert main._contact_name({"display_name": "Маша", "contact_alias": "a"}) == "Маша"


# ── _split_long_text ──────────────────────────────────────────────────────────

def test_split_long_text_short_stays_single():
    assert main._split_long_text("коротко") == ["коротко"]


def test_split_long_text_respects_limit_and_covers_content():
    text = "\n\n".join("абв" * 4 for _ in range(6))  # 6 абзацев по 12 символов
    parts = main._split_long_text(text, limit=20)
    assert len(parts) > 1
    assert all(len(p) <= 20 for p in parts)
    joined = "".join(p.replace("\n", "") for p in parts)
    assert joined == text.replace("\n", "")


# ── _style_cache_key ──────────────────────────────────────────────────────────

def test_style_cache_key_deterministic_and_sensitive():
    k1 = main._style_cache_key("reply", "flirt", "привет", "card-A", "inter-A")
    k2 = main._style_cache_key("reply", "flirt", "привет", "card-A", "inter-A")
    assert k1 == k2 and len(k1) == 64
    # смена карточки стиля меняет ключ (авто-инвалидация)
    assert main._style_cache_key("reply", "flirt", "привет", "card-B", "inter-A") != k1
    assert main._style_cache_key("rewrite", "flirt", "привет", "card-A", "inter-A") != k1


# ── _last_incoming_line ───────────────────────────────────────────────────────

def test_last_incoming_line():
    assert main._last_incoming_line("Она: привет\nЯ: норм\nОна: а ты куда?") == "Она: а ты куда?"
    assert main._last_incoming_line("Собеседник: привет\nЯ: норм\nЯ: ага") == "Собеседник: привет"
    assert main._last_incoming_line("Я: норм\nСобеседник: а ты куда?\nЯ: потом скажу") == "Собеседник: а ты куда?"
    assert main._last_incoming_line("одна строка") == "одна строка"
    assert main._last_incoming_line("текст\n\n   \n") == "текст"
    assert main._last_incoming_line("") == ""


# _auto_style_for_ctx удалена вместе со старой style_pick_kb-инфраструктурой
# (точечный выбор стиля/«Другой тон» убраны — см. main.py).


# ── _reply_data_signals ───────────────────────────────────────────────────────

def test_reply_data_signals_stage_from_totals_plus_situation():
    samples = {
        "features_summary": "Пользователь: 200 сообщ., длина 40. Собеседник: 190 сообщ., длина 30.",
        "my_sample": ["a"], "contact_sample": ["b"],   # длины малы, но тоталы из сводки главнее
    }
    sig = main._reply_data_signals(samples, "ок")
    assert sig is not None
    assert "давняя переписка" in sig          # стадия по реальным тоталам (390)
    assert "сух" in sig                        # сухая реплика «ок»


def test_reply_data_signals_none_when_nothing():
    # нет семплов и обычная реплика → сигналов нет
    assert main._reply_data_signals(None, "расскажи как прошёл день?") is None


# ── _format_blocks ────────────────────────────────────────────────────────────

def test_format_blocks():
    out = main._format_blocks([{"observation": "наб", "mechanism": "мех", "action": "дей"}])
    assert "🔍 наб" in out and "⚙️ мех" in out and "🎯 дей" in out
