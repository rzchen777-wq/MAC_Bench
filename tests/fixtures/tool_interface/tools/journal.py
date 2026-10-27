"""Durable call receipts: duplicate request IDs never repeat a native mutation."""
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from mac_bench.construction.realize.tools.contracts import ToolError, encoded


class CallJournal:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = RLock()
        self.db.execute('''CREATE TABLE IF NOT EXISTS tool_calls (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT UNIQUE, world TEXT,
            actor TEXT, role TEXT, request_id TEXT, tool TEXT, arguments TEXT,
            started TEXT, finished TEXT, result TEXT,
            UNIQUE(world, actor, request_id))''')
        self.db.commit()

    def begin(self, world, actor, role, request_id, tool, arguments):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                old = self.db.execute('SELECT call_id, role, tool, arguments, result FROM tool_calls WHERE world=? AND actor=? AND request_id=?',
                                      (world, actor, request_id)).fetchone()
                if old:
                    if old[1:4] != (role, tool, encoded(arguments)):
                        raise ToolError('request_id_conflict', 'This request_id was already used with different arguments, tool or role.')
                    cached = json.loads(old[4]) if old[4] is not None else {
                        'call_id': old[0], 'status': 'unknown', 'data': None,
                        'error': {'code': 'unfinished_call', 'message': 'The earlier call has no final receipt. Read native state before taking further action.'}}
                    self.db.commit()
                    return old[0], cached
                call_id = uuid4().hex
                self.db.execute('INSERT INTO tool_calls(call_id,world,actor,role,request_id,tool,arguments,started) VALUES(?,?,?,?,?,?,?,?)',
                    (call_id, world, actor, role, request_id, tool, encoded(arguments), datetime.now(timezone.utc).isoformat()))
                self.db.commit()
                return call_id, None
            except Exception:
                self.db.rollback()
                raise

    def finish(self, call_id, result):
        with self.lock:
            self.db.execute('UPDATE tool_calls SET finished=?, result=? WHERE call_id=?',
                            (datetime.now(timezone.utc).isoformat(), encoded(result), call_id))
            self.db.commit()

    def receipt(self, world, actor, role, call_id):
        """Only this actor's saved public receipt, never another actor or arguments."""
        with self.lock:
            row = self.db.execute(
                'SELECT result, started FROM tool_calls WHERE world=? AND actor=? AND role=? AND call_id=?',
                (world, actor, role, call_id)).fetchone()
            if not row:
                raise ToolError('result_unavailable', 'No saved result is accessible under this ID in this session.')
            if row[0] is None:
                raise ToolError('result_unfinished', 'This call has no final receipt. Inspect native state; do not repeat an uncertain write.')
            return json.loads(row[0]), row[1]

    def records(self, world):
        """Maintainer interface; never registered as an agent tool."""
        with self.lock:
            self.db.row_factory = sqlite3.Row
            rows = [dict(row) for row in self.db.execute('SELECT * FROM tool_calls WHERE world=? ORDER BY sequence', (world,))]
            self.db.row_factory = None
            return deepcopy(rows)

    def close(self):
        self.db.close()
