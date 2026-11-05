"""Однократное получение публичного каталога эмодзи, не вызывается меню.

Запуск из корня: py -3.13 -X utf8 -m tools.fetch_ui_emoji.
Токен берётся из config и не выводится; результаты — во временной папке.
"""
import json
import tempfile
from pathlib import Path

import httpx

from config import BOT_TOKEN

PACKS = ("BirthdayCollection", "NewsEmoji", "RestrictedEmoji", "MadEmoji2")


def main():
    catalog = {}
    with httpx.Client(trust_env=False, timeout=30) as client:
        for name in PACKS:
            try:
                result = client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/getStickerSet", json={"name": name}).json()
            except Exception as exc:
                print(name, "ошибка сети:", type(exc).__name__)
                continue
            if not result.get("ok"):
                print(name, "Telegram:", result.get("description", "ошибка"))
                continue
            catalog[name] = result["result"]
            stickers = result["result"]["stickers"]
            print(name, "получено:", len(stickers))
            print("Анимированных:", sum(s.get("is_animated", False) for s in stickers),
                  "видео:", sum(s.get("is_video", False) for s in stickers))
    target = Path(tempfile.gettempdir()) / "cueme_ui_emoji_catalog.json"
    target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Каталог:", target)


if __name__ == "__main__":
    main()
