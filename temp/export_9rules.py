# -*- coding: utf-8 -*-
"""2026년 5월 개정(좌표 재구성 추출) 9건의 규정명 목록 및 개별 텍스트 파일 저장"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RULES = [
    "교무위원회 규정",
    "정년보장 교원임용 심사위원회 규정",
    "직원징계청원위원회 규정",
    "정보화위원회 규정",
    "직인관리 규정",
    "정보보안 규정",
    "감염병 관리 규정",
    "IR센터 규정",
    "장학금 지급 규정",
]

OUT_DIR = Path(__file__).resolve().parent / "2026년 5월 개정 9건"
OUT_DIR.mkdir(exist_ok=True)

conn = sqlite3.connect("regulations.db")

list_lines = []
for i, name in enumerate(RULES, 1):
    row = conn.execute(
        "SELECT 분류명, 규정번호, 개정일자, 텍스트추출내용 "
        "FROM regulations WHERE 규정명 = ?", (name,)).fetchone()
    if row is None:
        print(f"! DB에 없음: {name}")
        continue
    cat, num, change_dt, text = row
    list_lines.append(f"{i}. {name} (분류: {cat}, 규정번호: {num}, 개정일자: {change_dt})")
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    out = OUT_DIR / f"{safe}.txt"
    out.write_text(text, encoding="utf-8")
    print(f"저장: {out.name} ({len(text):,}자)")

list_path = OUT_DIR / "2026년 5월 개정 9건 목록.txt"
list_path.write_text("\n".join(list_lines) + "\n", encoding="utf-8")
print(f"저장: {list_path.name}")
conn.close()
