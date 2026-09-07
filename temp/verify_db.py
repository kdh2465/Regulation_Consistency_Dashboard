# -*- coding: utf-8 -*-
"""DB 및 통합 텍스트 검증용 스크립트"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
conn = sqlite3.connect("regulations.db")

n, = conn.execute("SELECT COUNT(*) FROM regulations").fetchone()
empty, = conn.execute("SELECT COUNT(*) FROM regulations WHERE 텍스트추출내용=''").fetchone()
print(f"레코드: {n} / 본문없음: {empty}")

cols = ["규정명", "분류명", "DB구성날짜", "규정번호", "규정버전", "규정파일링크",
        "제정일자", "개정일자", "조회수", "관리부서", "관련부서"]
row = conn.execute(
    "SELECT " + ",".join(cols) + ", substr(텍스트추출내용,1,250) "
    "FROM regulations WHERE 규정명='학교법인 한양학원 정관'").fetchone()
for k, v in zip(cols + ["본문(앞250자)"], row):
    print(f"{k}: {v}")

print("\n--- 좌표 재구성 규정 샘플(교무위원회 규정, 앞 400자) ---")
t, = conn.execute(
    "SELECT substr(텍스트추출내용,1,400) FROM regulations WHERE 규정명='교무위원회 규정'").fetchone()
print(t)

print("\n--- 통합 파일 구분자 검증 ---")
merged = open("228개의 규정 통합.txt", encoding="utf-8").read()
print("총 길이:", len(merged))
print("규정 블록 수(\\n\\n\\n 분할):", len(merged.split("\n\n\n")))
conn.close()
