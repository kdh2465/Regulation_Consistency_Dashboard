# -*- coding: utf-8 -*-
"""
FactChat 챗봇 호출 스크립트
- .env 의 FACTCHAT_API_KEY 를 읽어 인증
- "분석모드:현행규정" 을 입력값으로 전달하고 응답 JSON 전체를 화면에 출력
"""
import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()
api_key = os.getenv("FACTCHAT_API_KEY")
if not api_key:
    sys.exit("오류: .env 에서 FACTCHAT_API_KEY 를 찾을 수 없습니다.")

client = OpenAI(
    api_key=api_key,
    base_url="https://factchat-cloud.mindlogic.ai/v1/gateway/chatbots/50628",
)

try:
    resp = client.chat.completions.create(
        model="chatbot",  # ignored by the server; the chatbot's configured model is used
        messages=[
            {"role": "user", "content": "분석모드:현행규정"},
        ],
    )
    print(json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))
except Exception as e:
    print(f"[현행규정 호출 실패] {e}")


# ---------------------------------------------------------------- 제개정규정 분석
# "분석모드: 제개정규정," 입력과 함께 PDF 2개의 본문 텍스트를 전달.
# FactChat 게이트웨이는 content 에 문자열만 허용(파일 첨부 미지원)하므로
# regulation_parser 의 추출기로 텍스트를 뽑아 메시지에 포함한다.

from pathlib import Path

import regulation_parser as rp

PDF_FILES = [
    Path(r"d:\Regulations_Parser\pdf_cache\IR센터 규정.pdf"),
    Path(r"d:\Regulations_Parser\pdf_cache\AI융합혁신센터 규정.pdf"),
]

parts = ["분석모드: 제개정규정,"]
for pdf in PDF_FILES:
    text = rp.RE_CTRL.sub("", rp.extract_pdf_text(pdf.read_bytes()))
    parts.append(f"[첨부파일: {pdf.name}]\n{text.strip()}")
prompt = "\n\n".join(parts)

try:
    resp2 = client.chat.completions.create(
        model="chatbot",
        messages=[
            {"role": "user", "content": prompt},
        ],
    )
    print(json.dumps(resp2.model_dump(), ensure_ascii=False, indent=2))
except Exception as e:
    print(f"[제개정규정 호출 실패] {e}")
