import storage


def test_ready_unique_index_skips_full_history_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot.db")
    storage.init_db()
    queries = []
    connect = storage._conn
    def traced():
        conn = connect()
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(storage, "_conn", traced)
    storage.init_db()
    assert not any(q.lstrip().upper().startswith("DELETE FROM BUSINESS_MESSAGES") for q in queries)


def test_missing_index_still_cleans_legacy_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot.db")
    storage.init_db()
    with storage._conn() as conn:
        conn.execute("DROP INDEX idx_biz_msg_unique")
        for mid in (1, 1, None, None):
            conn.execute("""INSERT INTO business_messages
                (connection_id,owner_user_id,chat_ref,direction,date,tg_message_id)
                VALUES('c','1','chat','in','2026-11-10',?)""", (mid,))
    storage.init_db()
    with storage._conn() as conn:
        assert conn.execute("select count(*) from business_messages").fetchone()[0] == 3
