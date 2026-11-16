"""Read-only feedback aggregation across current and legacy database snapshots."""
from __future__ import annotations

import sqlite3
from typing import Any


def feedback_report(connection: sqlite3.Connection, chat_id: int | None = None) -> dict[str, Any]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'feedback_events' not in tables:
        return {'up': 0, 'down': 0, 'items': []}
    columns = {row[1] for row in connection.execute('PRAGMA table_info(feedback_events)')}
    origins = []
    if 'sends' in tables and 'decision_id' in columns:
        origins.append(("sends", "decision_id", "NULL AS action_id, f.decision_id"))
    if 'actions' in tables and 'action_id' in columns:
        origins.append(("actions", "action_id", "f.action_id, NULL AS decision_id"))
    items = []
    for table, identity, identities in origins:
        sql = (f"SELECT {identities}, s.chat_id, s.topic_id, s.telegram_message_id, s.text,"
               " SUM(CASE WHEN f.new_vote='up' THEN 1 ELSE 0 END)"
               " - SUM(CASE WHEN f.old_vote='up' THEN 1 ELSE 0 END) AS up,"
               " SUM(CASE WHEN f.new_vote='down' THEN 1 ELSE 0 END)"
               " - SUM(CASE WHEN f.old_vote='down' THEN 1 ELSE 0 END) AS down,"
               " MAX(f.created_at) AS latest_at"
               f" FROM feedback_events f JOIN {table} s ON s.{identity}=f.{identity}"
               " AND s.chat_id=f.chat_id AND s.telegram_message_id=f.message_id"
               " WHERE s.status='sent'")
        params = ()
        if chat_id is not None:
            sql += ' AND s.chat_id=?'
            params = (chat_id,)
        sql += f' GROUP BY f.{identity}, s.chat_id, s.topic_id, s.telegram_message_id, s.text'
        cursor = connection.execute(sql, params)
        names = [description[0] for description in cursor.description]
        for values in cursor:
            row = dict(zip(names, values))
            items.append(dict(action_id=row['action_id'], decision_id=row['decision_id'],
                              chat_id=row['chat_id'], topic_id=row['topic_id'],
                              message_id=row['telegram_message_id'], text=row['text'],
                              up=int(row['up'] or 0), down=int(row['down'] or 0), latest_at=row['latest_at']))
    items.sort(key=lambda item: (-item['latest_at'], item['chat_id'], item['message_id']))
    return dict(up=sum(item['up'] for item in items), down=sum(item['down'] for item in items), items=items)
