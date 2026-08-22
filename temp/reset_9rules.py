# -*- coding: utf-8 -*-
"""좌표 재구성 9건의 레코드를 삭제하여 파서 재실행 시 재처리되게 한다."""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
RULES = [
    "교무위원회 규정", "정년보장 교원임용 심사위원회 규정", "직원징계청원위원회 규정",
    "정보화위원회 규정", "직인관리 규정", "정보보안 규정", "감염병 관리 규정",
    "IR센터 규정", "장학금 지급 규정",
]
conn = sqlite3.connect("regulations.db")
for name in RULES:
    cur = conn.execute("DELETE FROM regulations WHERE 규정명 = ?", (name,))
    print(f"삭제({cur.rowcount}): {name}")
conn.commit()
conn.close()
