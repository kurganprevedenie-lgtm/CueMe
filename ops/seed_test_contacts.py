"""Тестовые контакты для ручной проверки постраничного списка (только на
тестовом боте / своём аккаунте).

    venv/bin/python ops/seed_test_contacts.py <telegram_id> 25    # создать «Тест 01..25»
    venv/bin/python ops/seed_test_contacts.py <telegram_id> --remove  # убрать их

Контакты создаются с original_from_id вида test_seed_NN — по нему же и
удаляются, чужие контакты скрипт не трогает. Нескольким даётся по
сообщению в JSON-истории, чтобы было видно сортировку «недавние первыми».
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # DB_PATH бота относительный
import storage  # noqa: E402

tid = sys.argv[1]
storage.init_db()
existing = [c for c in storage.list_contacts(tid) if str(c["original_from_id"] or "").startswith("test_seed_")]

if "--remove" in sys.argv:
    for c in existing:
        storage.delete_contact_data(tid, c["id"])
    print(f"удалено тестовых контактов: {len(existing)}")
    sys.exit()

n = int(sys.argv[2])
storage.upsert_user(tid, f"user{tid}")
now = datetime.now()
for i in range(1, n + 1):
    cid = storage.get_or_create_contact(tid, f"test_seed_{i:02d}", f"Тест {i:02d}")
    if i % 5 == 0:  # каждый пятый — «недавняя переписка», чем больше номер, тем свежее
        storage.save_imported_messages(cid, [{"direction": "in", "text": "привет",
                                              "date": (now - timedelta(hours=n - i)).isoformat()}])
print(f"создано/есть тестовых контактов: {n}. Убрать: --remove")
