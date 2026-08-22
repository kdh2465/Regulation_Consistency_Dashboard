# -*- coding: utf-8 -*-
"""
한양여자대학교 그룹웨어 규정집 파서
- https://gw.hywoman.ac.kr/ekp/ruleweb/index.do 의 규정 트리를 순회하며
  규정 메타데이터 + PDF 본문 텍스트를 SQLite DB(regulations.db)에 저장
- 재실행 시: 신규 규정 insert, 개정일자 > DB구성날짜 인 규정 update
- 마지막으로 전체 텍스트를 "{N}개의 규정 통합.txt" 로 통합
"""

import io
import re
import sys
import time
import sqlite3
import datetime
from pathlib import Path

import requests
import pymupdf

# find_tables() 사용 시 pymupdf_layout 패키지 추천 안내문 출력 억제
pymupdf.no_recommend_layout()

BASE = "https://gw.hywoman.ac.kr"
CTX = BASE + "/ekp"
TREE_URL = CTX + "/ajax/rule/getRuleTrees.do"
INFO_URL = CTX + "/ajax/rule/getRuleAllInfo.do"
DOWNLOAD_URL = CTX + "/file/fileDownloadDrm.do?serverFileDir="
ROOT_ID = "root"

WORK_DIR = Path(__file__).resolve().parent
DB_PATH = WORK_DIR / "regulations.db"
DELETE_DB_PATH = WORK_DIR / "regulations_delete.db"
PDF_CACHE = WORK_DIR / "pdf_cache"
RUN_LOG_DIR = WORK_DIR / "run_log"

REQUEST_DELAY = 0.2  # 서버 부하 방지용 요청 간격(초)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": CTX + "/ruleweb/index.do",
})


_log_file = None  # main()에서 run_log 파일이 열리면 콘솔과 파일에 동시 기록


def log(msg):
    print(msg, flush=True)
    if _log_file is not None:
        _log_file.write(str(msg) + "\n")
        _log_file.flush()


# ---------------------------------------------------------------- 웹 API

def fetch_tree_children(node_id, node_type=None):
    """규정 트리 노드의 자식 목록을 가져온다."""
    data = {"id": node_id}
    if node_type:
        data["type"] = node_type
    time.sleep(REQUEST_DELAY)
    r = session.post(TREE_URL, data=data, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_rule_info(rule_id):
    """규정 상세 정보(규정정보 + 규정이력)를 가져온다."""
    time.sleep(REQUEST_DELAY)
    r = session.post(INFO_URL, data={"ruleId": rule_id}, timeout=30)
    r.raise_for_status()
    res = r.json()
    if not res.get("success"):
        raise RuntimeError(f"getRuleAllInfo 실패: {res.get('errorMap')}")
    return res["data"]


def download_rule_file(file_path, file_name):
    """규정 파일을 다운로드하여 bytes 로 반환한다."""
    url = DOWNLOAD_URL + file_path + "&clientFileName=" + requests.utils.quote(file_name or "file")
    time.sleep(REQUEST_DELAY)
    r = session.get(url, timeout=120)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------- 텍스트 추출

# 장 제목: "제 1 장 총 칙" (줄 시작, 비교적 짧은 줄만 인정)
RE_CHAPTER = re.compile(r"^제\s*\d+\s*장\b")
# 절 제목: "제 1 절 자 산"
RE_SECTION = re.compile(r"^제\s*\d+\s*절\b")
# 조 시작: "제1조(목적)", "제3조의2(…)", "제3조의2《삭제》", "제4조 (주소)"
RE_ARTICLE = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*[\(（《〈\[]")
RE_ARTICLE_DELETED = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*[《〈]?\s*삭\s*제")
# 부칙: "부 칙", "부칙 (2020. 1. 1.)"
RE_ADDENDUM = re.compile(r"^부\s*칙\b|^부\s*칙\s*[\(（<]")
# 별표/별지 서식: "[별표 1]", "[별지 제1호 서식]"
RE_ATTACH = re.compile(r"^[\[［]\s*별\s*[표지]")
# 본문 시작점: 제1장 또는 제1조
RE_BODY_START = re.compile(r"제\s*1\s*장\b|제\s*1\s*조(?:의\s*\d+)?\s*[\(（《]")
# 페이지 번호 등 노이즈 줄
RE_NOISE = re.compile(r"^[-‐–—\s]*\d+\s*[-‐–—\s]*$|^[ivxlcIVXLC]+$")
# PDF 텍스트 레이어에 섞여 나오는 제어문자(U+0001 등, 개행/탭 제외)
RE_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def extract_page_tables(page):
    """
    페이지에서 표를 감지하여 [(Rect, 한 줄 직렬화 텍스트)] 목록을 반환한다.
    셀은 ' | ', 행은 ' ; ' 로 구분하고 전체를 [표] ... [표끝] 로 감싼다.
    2행 2열 이상만 표로 인정한다(단일 박스/테두리 오탐 방지).
    """
    tables = []
    try:
        found = page.find_tables()
    except Exception:
        return tables
    for tab in found.tables:
        if tab.row_count < 2 or tab.col_count < 2:
            continue
        rows_out = []
        for row in tab.extract():
            cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in row]
            if any(cells):
                rows_out.append(" | ".join(cells))
        if rows_out:
            tables.append((pymupdf.Rect(tab.bbox),
                           "[표] " + " ; ".join(rows_out) + " [표끝]"))
    return tables


def extract_pdf_text(pdf_bytes):
    """
    PDF 바이트에서 페이지별 텍스트를 추출해 이어붙인다.
    표 영역은 평문 추출에서 제외하고, 구조를 보존한 직렬화 텍스트([표] ...)를
    해당 위치에 삽입한다.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for page in doc:
        tables = extract_page_tables(page)
        if not tables:
            parts.append(page.get_text("text"))
            continue
        items = []
        for x0, y0, x1, y1, btext, _bno, btype in page.get_text("blocks"):
            if btype != 0:  # 이미지 블록 제외
                continue
            center = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
            if any(rect.contains(center) for rect, _t in tables):
                continue  # 표 내부 텍스트는 직렬화된 표 텍스트로 대체
            items.append((y0, x0, btext.rstrip("\n")))
        for rect, ttext in tables:
            items.append((rect.y0, rect.x0, ttext))
        items.sort(key=lambda t: (t[0], t[1]))
        parts.append("\n".join(t[2] for t in items))
    doc.close()
    return "\n".join(parts)


def extract_pdf_text_charsort(pdf_bytes):
    """
    텍스트 레이어의 추출 순서가 깨진 PDF(일부 HWP 변환본)용 폴백.
    글자별 bbox 좌표를 이용해 시각적 순서(위->아래, 왼쪽->오른쪽)로 재구성한다.
    이런 PDF는 공백이 '&', '!' 글리프로 잘못 매핑되어 있어 공백으로 치환한다.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages_out = []
    for page in doc:
        tables = extract_page_tables(page)
        raw = page.get_text("rawdict")
        chars = []
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for ch in span.get("chars", []):
                        x0, y0, x1, y1 = ch["bbox"]
                        center = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
                        if any(rect.contains(center) for rect, _t in tables):
                            continue  # 표 내부 글자는 직렬화된 표 텍스트로 대체
                        chars.append(((y0 + y1) / 2, x0, ch["c"]))
        chars.sort(key=lambda c: (c[0], c[1]))
        lines = []  # (y, [(x, c)])
        cur_y = None
        cur = []
        for y, x, c in chars:
            if cur_y is None or abs(y - cur_y) > 3:
                if cur:
                    lines.append((cur_y, cur))
                cur = []
                cur_y = y
            cur.append((x, c))
        if cur:
            lines.append((cur_y, cur))
        items = []
        for y, ln in lines:
            ln.sort(key=lambda t: t[0])
            items.append((y, "".join(c for _x, c in ln)))
        for rect, ttext in tables:
            items.append((rect.y0, ttext))
        items.sort(key=lambda t: t[0])
        pages_out.append("\n".join(t[1] for t in items))
    doc.close()
    text = "\n".join(pages_out)
    # 깨진 공백 글리프 치환: 문서마다 대체 글리프가 다름('&', '!', '$' 등).
    # 정상적인 규정 문서에서는 거의 안 쓰이는 특수문자가 비정상적으로
    # 높은 빈도(0.5% 이상)로 나타나면 공백 오매핑으로 판단해 치환한다.
    # 표 직렬화 줄([표] ...)은 빈도 계산에서 제외하고, 구분자로 쓰는
    # '|' 등의 기호는 표 줄 안에서 치환하지 않는다.
    all_lines = text.splitlines()
    plain = "\n".join(l for l in all_lines if not l.startswith("[표]"))
    total = max(len(plain), 1)
    for sym in "&!$#@%^*~`|=+\\":
        if plain.count(sym) / total > 0.005:
            all_lines = [l if l.startswith("[표]") and sym in "|;"
                         else l.replace(sym, " ") for l in all_lines]
    return "\n".join(all_lines)


def is_chapter_line(line):
    return bool(RE_CHAPTER.match(line)) and len(line) <= 50 and "조(" not in line


def is_section_line(line):
    return bool(RE_SECTION.match(line)) and len(line) <= 50


def is_article_line(line):
    return bool(RE_ARTICLE.match(line) or RE_ARTICLE_DELETED.match(line))


def is_addendum_line(line):
    return bool(RE_ADDENDUM.match(line)) and len(line) <= 60


def is_attach_line(line):
    """'[별표 1] ...', '[별지 제1호 서식] ...' 형태의 별표/별지 제목 줄."""
    return bool(RE_ATTACH.match(line)) and len(line) <= 100


def normalize_segment(lines):
    """세그먼트 내부의 줄들을 하나의 문자열로 합친다(줄바꿈 -> 공백)."""
    text = " ".join(l.strip() for l in lines if l.strip())
    return re.sub(r"\s+", " ", text).strip()


def parse_rule_text(raw_text, rule_name):
    """
    규정 원문 텍스트를 파싱하여
    - 제1장/제1조부터 시작
    - 장 사이 \n\n, 조 사이 \n
    - 모든 장/조 앞에 (규정명) 삽입
    한 텍스트를 반환한다.
    """
    raw_text = RE_CTRL.sub("", raw_text)
    m = RE_BODY_START.search(raw_text)
    if not m:
        return ""
    body = raw_text[m.start():]

    lines = [l for l in body.splitlines() if l.strip() and not RE_NOISE.match(l.strip())]

    # (type, [lines]) 세그먼트로 분할. type: chapter | article
    segments = []
    current = None
    for line in lines:
        stripped = line.strip()
        if is_chapter_line(stripped) or is_addendum_line(stripped) or is_attach_line(stripped):
            current = ("chapter", [stripped])
            segments.append(current)
        elif is_section_line(stripped):
            # 절 제목은 장과 동일하게 취급하되 조 구분(\n)만 사용
            current = ("article", [stripped])
            segments.append(current)
        elif is_article_line(stripped):
            current = ("article", [stripped])
            segments.append(current)
        else:
            if current is None:
                current = ("article", [stripped])
                segments.append(current)
            else:
                current[1].append(stripped)

    out = []
    prefix = f"({rule_name})"
    for i, (seg_type, seg_lines) in enumerate(segments):
        text = normalize_segment(seg_lines)
        if not text:
            continue
        piece = f"{prefix} {text}"
        if i == 0:
            out.append(piece)
        elif seg_type == "chapter":
            out.append("\n\n" + piece)
        else:
            out.append("\n" + piece)
    return "".join(out)


# ---------------------------------------------------------------- DB

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regulations (
            규정명 TEXT PRIMARY KEY,
            분류명 TEXT,
            DB구성날짜 TEXT,
            규정번호 TEXT,
            규정버전 INTEGER,
            규정파일링크 TEXT,
            제정일자 TEXT,
            개정일자 TEXT,
            조회수 INTEGER,
            관리부서 TEXT,
            관련부서 TEXT,
            텍스트추출내용 TEXT
        )
    """)
    conn.commit()


def get_existing(conn, rule_name):
    cur = conn.execute(
        "SELECT DB구성날짜, 개정일자, 텍스트추출내용 FROM regulations WHERE 규정명 = ?",
        (rule_name,))
    return cur.fetchone()


def upsert(conn, record):
    conn.execute("""
        INSERT INTO regulations
            (규정명, 분류명, DB구성날짜, 규정번호, 규정버전, 규정파일링크,
             제정일자, 개정일자, 조회수, 관리부서, 관련부서, 텍스트추출내용)
        VALUES (:규정명, :분류명, :DB구성날짜, :규정번호, :규정버전, :규정파일링크,
                :제정일자, :개정일자, :조회수, :관리부서, :관련부서, :텍스트추출내용)
        ON CONFLICT(규정명) DO UPDATE SET
            분류명 = excluded.분류명,
            DB구성날짜 = excluded.DB구성날짜,
            규정번호 = excluded.규정번호,
            규정버전 = excluded.규정버전,
            규정파일링크 = excluded.규정파일링크,
            제정일자 = excluded.제정일자,
            개정일자 = excluded.개정일자,
            조회수 = excluded.조회수,
            관리부서 = excluded.관리부서,
            관련부서 = excluded.관련부서,
            텍스트추출내용 = excluded.텍스트추출내용
    """, record)
    conn.commit()


def move_removed_regulations(conn, seen_names, today):
    """
    사이트 규정 트리에서 사라진 규정을 regulations.db 에서 삭제하고
    regulations_delete.db 로 이동시킨다. 이동 건수를 반환한다.
    """
    cur = conn.execute("SELECT 규정명 FROM regulations")
    removed = [r[0] for r in cur.fetchall() if r[0] not in seen_names]
    if not removed:
        return 0

    del_conn = sqlite3.connect(DELETE_DB_PATH)
    del_conn.execute("""
        CREATE TABLE IF NOT EXISTS regulations (
            규정명 TEXT PRIMARY KEY,
            분류명 TEXT,
            DB구성날짜 TEXT,
            규정번호 TEXT,
            규정버전 INTEGER,
            규정파일링크 TEXT,
            제정일자 TEXT,
            개정일자 TEXT,
            조회수 INTEGER,
            관리부서 TEXT,
            관련부서 TEXT,
            텍스트추출내용 TEXT,
            삭제날짜 TEXT
        )
    """)
    for name in removed:
        row = conn.execute(
            "SELECT * FROM regulations WHERE 규정명 = ?", (name,)).fetchone()
        del_conn.execute("""
            INSERT OR REPLACE INTO regulations
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, row + (today,))
        conn.execute("DELETE FROM regulations WHERE 규정명 = ?", (name,))
        log(f"[삭제] 트리에서 사라진 규정 이동: {name}")
    del_conn.commit()
    del_conn.close()
    conn.commit()
    return len(removed)


# ---------------------------------------------------------------- 트리 순회

def strip_rule_number(text):
    """'1-0-1 학교법인 한양학원 정관' -> '학교법인 한양학원 정관'"""
    return re.sub(r"^\s*\d+(?:-\d+)*\s+", "", text).strip()


def collect_regulations():
    """
    트리를 순회하여 [(분류명, 규정노드), ...] 목록을 만든다.
    분류명 = 대학규정 바로 아래 첫 단계 노드의 이름.
    """
    results = []
    roots = fetch_tree_children(ROOT_ID)
    for root in roots:  # 예: 대학규정
        log(f"[트리] 최상위: {root['text']}")
        categories = fetch_tree_children(root["id"], root["type"])
        for cat in categories:
            cat_name = cat["text"].strip()
            log(f"[트리] 분류: {cat_name}")
            stack = [(cat, cat["type"])]
            while stack:
                node, ntype = stack.pop(0)
                if node["type"] == "regulation":
                    results.append((cat_name, node))
                    continue
                if node.get("children"):
                    children = fetch_tree_children(node["id"], node["type"])
                    for child in children:
                        if child["type"] == "regulation":
                            results.append((cat_name, child))
                        elif child.get("children"):
                            stack.append((child, child["type"]))
    return results


# ---------------------------------------------------------------- 메인 처리

def get_rule_text(info, rule_name):
    """규정 파일을 내려받아 본문 텍스트를 추출/파싱한다."""
    file_path = info.get("ruleFilePath")
    file_name = info.get("ruleFileNm") or ""
    file_type = (info.get("ruleFileType") or "").lower()
    if not file_path:
        log(f"    ! 규정파일 없음: {rule_name}")
        return ""

    pdf_bytes = None
    try:
        content = download_rule_file(file_path, file_name)
        if content[:5] == b"%PDF-":
            pdf_bytes = content
        elif file_type == "pdf":
            log(f"    ! PDF 시그니처 불일치: {rule_name}")
    except Exception as e:
        log(f"    ! 파일 다운로드 실패({rule_name}): {e}")
        return ""

    if pdf_bytes is None:
        log(f"    ! PDF가 아닌 파일 형식({file_type}): {rule_name} - 텍스트 추출 생략")
        return ""

    # PDF 캐시 저장(디버깅/재검증용)
    PDF_CACHE.mkdir(exist_ok=True)
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", rule_name)
    (PDF_CACHE / f"{safe_name}.pdf").write_bytes(pdf_bytes)

    try:
        raw = extract_pdf_text(pdf_bytes)
    except Exception as e:
        log(f"    ! PDF 텍스트 추출 실패({rule_name}): {e}")
        return ""

    parsed = parse_rule_text(raw, rule_name)
    if not parsed:
        # 텍스트 순서가 깨진 PDF: 글자 좌표 기반 재구성으로 재시도
        try:
            raw2 = extract_pdf_text_charsort(pdf_bytes)
            parsed = parse_rule_text(raw2, rule_name)
            if parsed:
                log(f"    * 좌표 기반 재구성으로 추출 성공: {rule_name}")
        except Exception as e:
            log(f"    ! 좌표 기반 재구성 실패({rule_name}): {e}")
    if not parsed:
        log(f"    ! 본문 시작점(제1장/제1조)을 찾지 못함: {rule_name}")
    return parsed


def build_record(cat_name, info, rule_name, text, today):
    file_path = info.get("ruleFilePath") or ""
    file_name = info.get("ruleFileNm") or ""
    file_link = (DOWNLOAD_URL + file_path +
                 "&clientFileName=" + requests.utils.quote(file_name)) if file_path else ""
    return {
        "규정명": rule_name,
        "분류명": cat_name,
        "DB구성날짜": today,
        "규정번호": info.get("ruleNumber") or "",
        "규정버전": info.get("ruleVersion"),
        "규정파일링크": file_link,
        "제정일자": info.get("createDt") or "",
        "개정일자": info.get("changeDt") or "",
        "조회수": info.get("readCnt"),
        "관리부서": info.get("manageDeptNm") or "",
        "관련부서": info.get("relevantDeptNm") or "",
        "텍스트추출내용": text,
    }


def rule_number_sort_key(num):
    """'4-3-1' 같은 규정번호를 자연수 배열로 정렬 키 생성."""
    parts = re.findall(r"\d+", num or "")
    return [int(p) for p in parts] if parts else [9999]


def export_merged_text(conn):
    cur = conn.execute(
        "SELECT 규정번호, 텍스트추출내용 FROM regulations WHERE 텍스트추출내용 != ''")
    rows = cur.fetchall()
    rows.sort(key=lambda r: rule_number_sort_key(r[0]))
    texts = [r[1] for r in rows]
    count = len(texts)
    today = datetime.date.today().strftime("%Y.%m.%d")
    out_path = WORK_DIR / f"한양여자대학교_{count}개의 규정통합({today}).txt"
    out_path.write_text("\n\n\n".join(texts), encoding="utf-8")
    log(f"[통합] {count}개 규정 -> {out_path.name}")
    return out_path


def export_rule_list(conn):
    """전체 규정 목록을 번호/분류명/규정명 순으로 규정목록.txt 에 저장한다.
    (순서는 DB 에 저장된 순서 = rowid 순)"""
    rows = conn.execute("SELECT 분류명, 규정명 FROM regulations").fetchall()
    lines = ["번호\t분류명\t규정명"]
    for i, (cat, name) in enumerate(rows, 1):
        lines.append(f"{i}\t{cat}\t{name}")
    out_path = WORK_DIR / "규정목록.txt"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"[목록] {len(rows)}개 규정 -> {out_path.name}")
    return out_path


# ---------------------------------------------------------------- 클러스터별 규정 저장

CLUSTER_FILE = WORK_DIR / "주제별 클러스터.txt"
CLUSTER_DIR = WORK_DIR / "regulationsByCluster"
CLUSTER_SPLIT_LIMIT = 10 * 1024  # 클러스터 txt 분할 기준(10KB, utf-8 바이트)


def _norm_name(s):
    """
    규정명 매칭용 정규화: 공백/가운뎃점/구두점/괄호류 제거 후
    연결어("에 관한", "및", "등", "의")를 제거한다.
    키워드와 규정명 양쪽에 동일하게 적용되므로 표기 차이를 흡수한다.
    """
    s = re.sub(r"[\s·\-–—()\[\]「」『』+.,/]", "", s)
    for w in ("에관한", "및", "등", "의"):
        s = s.replace(w, "")
    return s


def _is_subsequence(needle, haystack):
    """needle 의 글자들이 순서대로 haystack 에 모두 나타나는지."""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _expand_cluster_item(item):
    """
    클러스터 파일의 규정 항목 하나를 매칭 키워드(정규화) 목록으로 확장한다.
    - "문서관리 규정(개인정보 처리 관련 조항)" -> 괄호 설명 제거본
    - "입학전형(관리·공정관리·회피제척)위원회" -> 접두어+각 대안(+접미어) 전개
    - "학점은행제·학점인정" -> 가운뎃점 기준 분리 항목 추가
    - "각종 ~", "~ 등 ..." 같은 수식어 제거
    """
    keywords = set()
    item = re.sub(r"^각종\s*", "", item.strip())
    m = re.match(r"^(.*)\(([^)]*)\)(.*)$", item)
    if m:
        prefix, alts, suffix = m.group(1), m.group(2), m.group(3)
        for alt in re.split(r"[·,]", alts):
            if alt.strip():
                keywords.add(_norm_name(prefix + alt + suffix))
                keywords.add(_norm_name(prefix + alt))
    base = re.sub(r"\([^)]*\)", "", item)  # 괄호 설명 제거
    keywords.add(_norm_name(base))
    for part in base.split("·"):  # 복수 규정을 ·로 묶은 항목 분리
        part = part.split(" 등 ")[0]  # "부트캠프 등 사업단 운영규정" -> "부트캠프"
        part = _norm_name(part)
        if len(part) >= 3:
            keywords.add(part)
    return {k for k in keywords if len(k) >= 2}


def _match_cluster(norm_name, keywords):
    """정규화된 규정명이 키워드 집합과 일치하는지(부분일치 + 부분수열) 판정."""
    for k in keywords:
        if k in norm_name or norm_name in k:
            return True
        if len(k) >= 4 and _is_subsequence(k, norm_name):
            return True
    return False


def parse_cluster_file():
    """주제별 클러스터.txt -> [(클러스터명, {키워드,...}), ...]"""
    lines = CLUSTER_FILE.read_text(encoding="utf-8").splitlines()
    header_re = re.compile(r"^([A-Z])\.\s*(.+?)\s*(?:—|-)\s*참고법령")
    clusters = []
    for idx, line in enumerate(lines):
        m = header_re.match(line.strip())
        if m and idx + 1 < len(lines):
            cluster_name = f"{m.group(1)}. {m.group(2)}"
            keywords = set()
            for item in lines[idx + 1].split(","):
                if item.strip():
                    keywords |= _expand_cluster_item(item.strip())
            clusters.append((cluster_name, keywords))
    return clusters


def _write_cluster_split(safe_name, texts):
    """
    클러스터 통합 텍스트를 파일로 저장한다. utf-8 기준 10KB 를 넘으면
    규정 경계에서 나누어 클러스터 ID 뒤에 넘버링한 파일로 분할 저장한다.
    예) A. 조직….txt 가 180KB -> A1. 조직….txt(10KB 이하) + A2. 조직….txt
    저장한 파일명 목록을 반환한다.
    """
    sep = "\n\n\n"
    sep_size = len(sep.encode("utf-8"))
    chunks, cur, cur_size = [], [], 0
    for text in texts:
        size = len(text.encode("utf-8"))
        if cur and cur_size + sep_size + size > CLUSTER_SPLIT_LIMIT:
            chunks.append(sep.join(cur))
            cur, cur_size = [text], size
        else:
            cur.append(text)
            cur_size += (sep_size if cur_size else 0) + size
    chunks.append(sep.join(cur))

    # 이전 실행이 남긴 같은 클러스터의 단일본/분할본 제거 (예: A. x.txt, A1. x.txt …)
    cid, _dot, rest = safe_name.partition(".")
    old_pat = re.compile(rf"^{re.escape(cid)}\d*\.{re.escape(rest)}$")
    for p in CLUSTER_DIR.glob("*.txt"):
        if old_pat.match(p.stem):
            p.unlink()

    if len(chunks) == 1:
        names = [f"{safe_name}.txt"]
    else:
        names = [f"{cid}{i}.{rest}.txt" for i in range(1, len(chunks) + 1)]
    written = []
    for name, chunk in zip(names, chunks):
        (CLUSTER_DIR / name).write_text(chunk, encoding="utf-8")
        written.append(f"{name}({len(chunk.encode('utf-8')) / 1024:.0f}KB)")
    return written


def export_regulations_by_cluster(conn):
    """
    주제별 클러스터.txt 를 참조하여 DB의 규정을 클러스터별로 분류하고,
    regulationsByCluster/<클러스터명>.txt 로 저장한다.
    파일이 10KB 를 넘으면 넘버링(A1, A2, …)하여 분할 저장한다.
    (규정명 표기 차이를 흡수하기 위해 정규화 후 부분일치로 매칭)
    """
    if not CLUSTER_FILE.exists():
        log(f"[클러스터] {CLUSTER_FILE.name} 없음 - 클러스터별 저장 생략")
        return
    clusters = parse_cluster_file()
    rows = conn.execute(
        "SELECT 규정명, 텍스트추출내용 FROM regulations WHERE 텍스트추출내용 != ''"
    ).fetchall()

    CLUSTER_DIR.mkdir(exist_ok=True)
    assigned = set()
    for cluster_name, keywords in clusters:
        matched = []
        for name, text in rows:
            if _match_cluster(_norm_name(name), keywords):
                matched.append((name, text))
                assigned.add(name)
        safe = re.sub(r'[\\/:*?"<>|]', "_", cluster_name)
        written = _write_cluster_split(safe, [t for _n, t in matched])
        log(f"[클러스터] {cluster_name}: {len(matched)}개 규정 -> {', '.join(written)}")

    unassigned = [name for name, _t in rows if name not in assigned]
    if unassigned:
        log(f"[클러스터] ! 어느 클러스터에도 속하지 않은 규정 {len(unassigned)}건: "
            + ", ".join(unassigned[:10]) + (" ..." if len(unassigned) > 10 else ""))


def main():
    global _log_file
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    today = datetime.date.today().isoformat()

    # 실행 로그 파일 (같은 날 재실행 시 이어서 기록)
    RUN_LOG_DIR.mkdir(exist_ok=True)
    log_path = RUN_LOG_DIR / f"run_log_{datetime.date.today().strftime('%Y.%m.%d')}.txt"
    _log_file = open(log_path, "a", encoding="utf-8")
    try:
        run_main(today)
    finally:
        _log_file.close()
        _log_file = None


def run_main(today):
    log(f"\n===== 실행 시작: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
    log(f"=== 규정 파싱 시작 (DB구성날짜: {today}) ===")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    regulations = collect_regulations()
    log(f"[트리] 규정 수집 완료: 총 {len(regulations)}건")

    n_insert = n_update = n_skip = n_fail = 0
    seen_names = set()  # 이번 실행에서 트리에 존재가 확인된 규정명
    for idx, (cat_name, node) in enumerate(regulations, 1):
        tree_name = strip_rule_number(node["text"])
        seen_names.add(tree_name)
        if node.get("deleteYn") == "Y":
            log(f"[{idx}/{len(regulations)}] 폐지규정 건너뜀: {tree_name}")
            n_skip += 1
            continue
        try:
            info = fetch_rule_info(node["id"])
        except Exception as e:
            log(f"[{idx}/{len(regulations)}] ! 상세정보 조회 실패({tree_name}): {e}")
            n_fail += 1
            continue

        rule_name = (info.get("ruleNm") or tree_name).strip()
        seen_names.add(rule_name)
        change_dt = (info.get("changeDt") or "").strip()

        existing = get_existing(conn, rule_name)
        if existing is not None:
            db_date, _old_change, old_text = existing
            # 개정일자가 DB구성날짜보다 나중인 경우 업데이트,
            # 본문 추출이 비어 있는 레코드는 재시도
            need_update = change_dt and db_date and change_dt > db_date
            need_retry = not (old_text or "").strip()
            if not (need_update or need_retry):
                log(f"[{idx}/{len(regulations)}] 변경 없음: {rule_name}")
                n_skip += 1
                continue
            action = "업데이트"
        else:
            action = "신규추가"

        text = get_rule_text(info, rule_name)
        record = build_record(cat_name, info, rule_name, text, today)
        upsert(conn, record)
        if action == "신규추가":
            n_insert += 1
        else:
            n_update += 1
        log(f"[{idx}/{len(regulations)}] {action}: {rule_name} "
            f"(분류: {cat_name}, 개정일: {change_dt}, 본문 {len(text):,}자)")

    log(f"=== 처리 결과: 신규 {n_insert}, 업데이트 {n_update}, "
        f"건너뜀 {n_skip}, 실패 {n_fail} ===")

    # 트리에서 사라진 규정 -> regulations_delete.db 로 이동
    # (조회 실패가 있으면 규정명 확인이 불완전하므로 오삭제 방지를 위해 생략)
    if n_fail == 0:
        n_moved = move_removed_regulations(conn, seen_names, today)
        if n_moved:
            log(f"[삭제] 총 {n_moved}건을 {DELETE_DB_PATH.name} 로 이동")
    else:
        log(f"[삭제] 조회 실패 {n_fail}건이 있어 사라진 규정 정리를 건너뜀")

    export_merged_text(conn)
    export_regulations_by_cluster(conn)
    export_rule_list(conn)
    conn.close()
    log("=== 완료 ===")


if __name__ == "__main__":
    main()
