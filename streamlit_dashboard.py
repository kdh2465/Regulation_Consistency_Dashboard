# -*- coding: utf-8 -*-
"""
한양여자대학교 규정 정합성 평가 대시보드 (Streamlit)
- 한양여자대학교_규정정합성_평가_대시보드_UI.html 과 동일한 디자인/구조
- 탭 1(현행규정 정합성): "분석모드:현행규정" 을 FactChat Agent 로 전달하고
  반환된 JSON(schema 1.1)을 파싱하여 각 영역에 대입
- 탭 2(제·개정규정 정합성): 업로드한 PDF 의 본문 텍스트를 추출하여
  "분석모드:제개정규정" 프롬프트와 함께 전송하고 반환 JSON 을 대입

실행: .venv\\Scripts\\python streamlit_dashboard.py
(python 으로 직접 실행하면 자동으로 `streamlit run` 으로 재시작한다)
"""
import contextlib
import datetime
import json
import os
import re
import sys

import streamlit as st
import streamlit.components.v1 as components
from dotenv import dotenv_values, load_dotenv
from openai import OpenAI

import regulation_parser as rp


def _running_under_streamlit():
    """streamlit run 컨텍스트에서 실행 중인지 여부."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx(suppress_warning=True) is not None
    except Exception:
        return False


if __name__ == "__main__" and not _running_under_streamlit():
    # `python streamlit_dashboard.py` 로 실행된 경우 `streamlit run` 으로 부트스트랩
    from streamlit.web import cli as stcli
    sys.argv = ["streamlit", "run", os.path.abspath(__file__)]
    sys.exit(stcli.main())

st.set_page_config(
    page_title="한양여자대학교 규정 정합성 평가 대시보드",
    layout="wide",
)

FACTCHAT_BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway/chatbots/50628"


# ---------------------------------------------------------------- Agent 호출
@st.cache_resource
def get_client():
    load_dotenv()
    try:  # secrets.toml 이 없거나 형식 오류여도 예외 대신 .env 폴백
        api_key = st.secrets.get("FACTCHAT_API_KEY")
    except Exception:
        api_key = None
    api_key = api_key or os.getenv("FACTCHAT_API_KEY")
    if not api_key:
        st.error("FACTCHAT_API_KEY 를 찾을 수 없습니다 "
                 "(`.streamlit/secrets.toml` 또는 `.env` 를 확인하세요).")
        st.stop()
    return OpenAI(api_key=api_key, base_url=FACTCHAT_BASE_URL)


# 마지막 Agent 응답 원문(디버깅용): 파싱 실패 시 여기서 전체 응답을 확인한다.
LAST_RESPONSE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "last_agent_response.txt")


def _parse_agent_json(content):
    """
    Agent 응답에서 JSON 객체를 관대하게 추출한다.
    - 마크다운 코드펜스 제거
    - JSON 앞뒤에 설명문 등 추가 텍스트가 붙어도 첫 번째 완전한
      JSON 객체만 파싱 (json.loads 의 'Extra data' 오류 방지)
    - JSON 을 찾지 못하면 응답 시작 부분을 포함한 오류를 발생시킨다.
    """
    content = (content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
    if not content:
        raise ValueError("Agent 응답이 비어 있습니다.")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        if start >= 0:
            try:
                obj, _end = json.JSONDecoder().raw_decode(content, start)
                return obj
            except json.JSONDecodeError:
                pass
        preview = re.sub(r"\s+", " ", content)[:300]
        raise ValueError(
            "Agent 응답이 JSON 형식이 아닙니다 "
            f"(전체 응답: last_agent_response.txt 참조). 응답 시작: {preview}")


def call_factchat(prompt):
    """FactChat Agent 를 호출하고 응답 본문을 JSON 으로 파싱해 반환한다."""
    resp = get_client().chat.completions.create(
        model="chatbot",  # 서버에서 무시됨(챗봇에 설정된 모델 사용)
        messages=[{"role": "user", "content": prompt}],
    )
    content = resp.choices[0].message.content
    try:  # 원문 저장(디버깅용) — 실패해도 분석은 계속
        with open(LAST_RESPONSE_PATH, "w", encoding="utf-8") as fp:
            fp.write(content or "")
    except OSError:
        pass
    return _parse_agent_json(content)


# ------------------------------------------------- Agent JSON -> 대시보드 데이터
def _fmt_regs(issue):
    """관련 내부 규정 문자열: university_sources + revision_sources."""
    parts = []
    for s in (issue.get("university_sources") or []) + (issue.get("revision_sources") or []):
        t = " ".join(x for x in [
            s.get("regulation_name", ""),
            s.get("article_number", ""),
            s.get("paragraph", ""),
        ] if x).strip()
        if t and t not in parts:
            parts.append(t)
    return ", ".join(parts) or "-"


def _fmt_laws(issue):
    """관련 상위법령 문자열: legal_sources."""
    parts = []
    for s in issue.get("legal_sources") or []:
        t = s.get("law_name", "")
        if s.get("article_number"):
            t += " " + s["article_number"]
        if s.get("article_title"):
            t += f"({s['article_title']})"
        if t and t not in parts:
            parts.append(t)
    return ", ".join(parts) or "해당 없음"


def _fmt_rec(issue):
    """수정 권고안 문자열: recommendation(dict 또는 str)."""
    rec = issue.get("recommendation")
    if isinstance(rec, dict):
        out = rec.get("summary", "")
        if rec.get("proposed_text"):
            out += f" — 제안 조문: {rec['proposed_text']}"
        return out or "-"
    return rec or "-"


def agent_json_to_dashboard_data(agent_json, mode="current"):
    """
    FactChat Agent 리턴 JSON(schema_version 1.1)의 issues[] 를
    대시보드 그룹(lr-inc/lr-rev/ir-inc/ir-rev) 구조로 변환한다.
    - comparison_type: law_to_* -> lr, university_to_* -> ir
    - status: inconsistent -> inc, review_required -> rev (그 외 제외)
    """
    if mode == "current":
        pair = ("상위법령 ↔ 대학규정", "대학규정 ↔ 대학규정")
    else:
        pair = ("상위법령 ↔ 제개정규정", "대학규정 ↔ 제개정규정")

    groups = {
        "lr-inc": {"key": "lr-inc", "title": f"{pair[0]} · 정합성 불일치", "type": "inc", "items": []},
        "lr-rev": {"key": "lr-rev", "title": f"{pair[0]} · 정합성 검토 필요", "type": "rev", "items": []},
        "ir-inc": {"key": "ir-inc", "title": f"{pair[1]} · 정합성 불일치", "type": "inc", "items": []},
        "ir-rev": {"key": "ir-rev", "title": f"{pair[1]} · 정합성 검토 필요", "type": "rev", "items": []},
    }
    for issue in agent_json.get("issues", []):
        status = issue.get("status")
        if status == "inconsistent":
            kind = "inc"
        elif status == "review_required":
            kind = "rev"
        else:
            continue  # consistent / not_applicable 은 표시하지 않음
        ctype = issue.get("comparison_type", "")
        side = "lr" if ctype.startswith("law_to") else "ir"
        try:
            level = max(1, min(5, int(issue.get("severity", 1))))
        except (TypeError, ValueError):
            level = 1
        groups[f"{side}-{kind}"]["items"].append({
            "code": issue.get("issue_id", ""),
            "title": issue.get("title", ""),
            "sub": issue.get("subtitle", ""),
            "level": level,
            "regs": _fmt_regs(issue),
            "law": _fmt_laws(issue),
            "basis": issue.get("reason", ""),
            "rec": _fmt_rec(issue),
            "completed": False,
        })
    return list(groups.values())


def show_warnings(agent_json):
    """분석 상태가 partial 이면 Agent 가 보고한 경고를 안내한다.
    최대 4줄 높이의 박스로 표시하고, 내용이 넘치면 우측 스크롤바로 확인한다.
    setting 에서 '부분 분석 안내 표시' 를 끄면 두 탭 모두 표시하지 않는다."""
    if not load_settings().get("show_partial", True):
        return
    if not agent_json or agent_json.get("analysis", {}).get("status") != "partial":
        return
    warnings = agent_json.get("retrieval_report", {}).get("warnings") or []
    if not warnings:
        return
    body = "<b>부분 분석(partial)</b>: " + "<br>".join(warnings)
    st.markdown(f'<div class="warn-box">{body}</div>', unsafe_allow_html=True)


def show_request_info(info):
    """API 로 전송한 입력 데이터의 형태(분석모드·클러스터·첨부파일명)를 표시한다."""
    if info:
        st.caption("**API 입력** — " + " · ".join(info))


def last_analyzed_html(ts):
    """분석 실행 버튼 상단에 표시할 최근 분석일(시간 포함) 라벨."""
    return f'<div class="last-analyzed">{"최근 분석일 " + ts if ts else "분석 대기"}</div>'


@contextlib.contextmanager
def overlay_spinner(message):
    """분석 진행 동안 원형 프로그레스바와 메시지를 화면 정중앙에 오버레이로 표시한다."""
    ph = st.empty()
    ph.markdown(
        f'<div class="overlay-spinner"><div class="overlay-card">'
        f'<div class="overlay-ring"></div><span>{message}</span></div></div>',
        unsafe_allow_html=True)
    try:
        yield
    finally:
        ph.empty()


# ---------------------------------------------------------------- 분석 결과 영구 저장
# 클러스터별 분석 결과(반환 JSON·대시보드 데이터·건수·최근 분석일 등)를 파일로 저장하고,
# 재실행/재접속 시 읽어와 화면에 맵핑한다. 다시 분석하면 해당 클러스터 값을 덮어쓴다.
STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis_store.json")


def _load_store():
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return {}
    return {}


def _save_store(store):
    """저장소 전체를 기록(임시파일 후 원자적 교체)."""
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(store, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def load_cluster_result(cluster_id):
    """저장된 클러스터별 분석 결과 반환(없으면 None)."""
    return _load_store().get("current", {}).get(cluster_id)


def save_cluster_result(cluster_id, entry):
    """클러스터별 분석 결과를 저장(기존 값 덮어쓰기)."""
    store = _load_store()
    store.setdefault("current", {})[cluster_id] = entry
    _save_store(store)


def load_revision_result():
    """제개정규정 탭의 마지막 저장 분석 결과 반환(없으면 None)."""
    rev = _load_store().get("revision", {})
    return (rev.get("entries") or {}).get(rev.get("last_key"))


def save_revision_result(key, entry):
    """제개정규정 분석 결과를 업로드 파일명 키로 저장하고 마지막 키를 갱신한다."""
    store = _load_store()
    rev = store.setdefault("revision", {})
    rev.setdefault("entries", {})[key] = entry  # 같은 파일 조합 재분석 시 덮어쓰기
    rev["last_key"] = key
    _save_store(store)


def delete_cluster_result(cluster_id):
    """현행규정 탭 초기화: 해당 클러스터의 저장 결과를 삭제한다."""
    store = _load_store()
    if store.get("current", {}).pop(cluster_id, None) is not None:
        _save_store(store)


def clear_revision_result():
    """제개정규정 탭 초기화: 마지막 표시 결과를 해제한다(과거 이력 entries 는 보존)."""
    store = _load_store()
    rev = store.get("revision") or {}
    if rev.get("last_key"):
        rev["last_key"] = None
        _save_store(store)


# ---------------------------------------------------------------- 앱 설정·비밀번호
# 설정(부분 분석 표시 여부)은 app_settings.json 에 저장한다.
# 비밀번호는 .env 와 .streamlit/secrets.toml 모두에 저장(없는 파일은 skip)하고,
# 조회 우선순위는 .env → .streamlit/secrets.toml 이다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
SECRETS_PATH = os.path.join(BASE_DIR, ".streamlit", "secrets.toml")
SETTINGS_PATH = os.path.join(BASE_DIR, "app_settings.json")
PW_ENV_KEYS = {
    "current": "CURRENT_ANALYSIS_PASSWORD",    # 현행규정 탭: 분석·초기화 비밀번호
    "revision": "REVISION_ANALYSIS_PASSWORD",  # 제·개정규정 탭: 분석·초기화 비밀번호
    "settings": "SETTINGS_PASSWORD",           # setting 진입 비밀번호(최초 2465)
}
PW_LABELS = {"current": "현행규정", "revision": "제·개정규정", "settings": "setting 진입"}
DEFAULT_SETTINGS_PASSWORD = "2465"  # setting 진입 비밀번호 미설정 시 최초값


def load_settings():
    """앱 설정 로드(없으면 기본값). show_partial: 부분 분석(파란 박스) 표시 여부."""
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return {}
    return {}


def save_settings(settings):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(settings, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def get_password(mode):
    """비밀번호 조회: .env 를 먼저 읽고 없으면 secrets.toml 을 읽는다.
    setting 진입 비밀번호는 어디에도 없으면 최초값(2465)을 사용한다."""
    key = PW_ENV_KEYS[mode]
    val = dotenv_values(ENV_PATH).get(key)  # 파일을 직접 읽어 설정 변경 즉시 반영
    if not val:
        try:  # secrets.toml 이 없거나 형식 오류여도 예외 대신 미설정 처리
            val = st.secrets.get(key)
        except Exception:
            val = None
    if not val and mode == "settings":
        val = DEFAULT_SETTINGS_PASSWORD
    return val or None


def _update_env(updates):
    """.env 파일에서 해당 키만 갱신(없으면 추가)하고 나머지 행은 보존한다.
    파일이 없으면 건너뛴다."""
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, "r", encoding="utf-8") as fp:
        lines = fp.read().splitlines()
    for key, value in updates.items():
        new_line = f"{key}={value}"
        for i, line in enumerate(lines):
            if line.strip().startswith(key + "="):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
    with open(ENV_PATH, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


def _update_secrets(updates):
    """.streamlit/secrets.toml 에서 해당 키만 갱신(없으면 최상위에 추가)하고
    나머지 행은 보존한다. 파일이 없으면 건너뛴다."""
    if not os.path.exists(SECRETS_PATH):
        return
    with open(SECRETS_PATH, "r", encoding="utf-8") as fp:
        lines = fp.read().splitlines()
    for key, value in updates.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        new_line = f'{key} = "{escaped}"'
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i, line in enumerate(lines):
            if pat.match(line):
                lines[i] = new_line
                break
        else:
            # [section] 아래에 붙지 않도록 최상위(첫 섹션 헤더 앞)에 추가한다.
            insert_at = next((i for i, ln in enumerate(lines)
                              if ln.strip().startswith("[")), len(lines))
            lines.insert(insert_at, new_line)
    with open(SECRETS_PATH, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


def update_passwords(updates):
    """비밀번호를 .env 와 .streamlit/secrets.toml 모두에 저장한다(없는 파일은 skip)."""
    _update_env(updates)
    _update_secrets(updates)


@st.dialog("비밀번호 확인")
def ask_password(mode, action):
    """동작 실행 전 비밀번호 확인. 일치하면 세션 플래그
    pw_verified_{mode}_{action} 를 세우고 rerun 하여 본문에서 해당 동작을 실행한다.
    - mode: current/revision(탭별 분석·초기화 공용) 또는 settings(setting 진입)
    - action: run(분석) / reset(초기화) / open(setting 열기)"""
    saved = get_password(mode)
    if not saved:
        st.error("비밀번호가 설정되어 있지 않습니다. 우측 상단 setting 에서 "
                 "비밀번호를 지정하거나 .env / secrets.toml 에 "
                 f"{PW_ENV_KEYS[mode]} 를 추가하세요.")
        return
    # form 으로 감싸 입력창에서 [엔터] 입력 시 확인 버튼이 눌리도록 한다.
    with st.form(f"pw_form_{mode}_{action}", border=False):
        pw = st.text_input(f"{PW_LABELS[mode]} 비밀번호", type="password",
                           key=f"pw_input_{mode}_{action}")
        submitted = st.form_submit_button("확인", type="primary", width="stretch")
    if submitted:
        if pw == saved:
            st.session_state[f"pw_verified_{mode}_{action}"] = True
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")


@st.dialog("설정", width="medium")
def settings_dialog():
    """우측 상단 setting: 부분 분석 표시 여부 + 탭별 분석 비밀번호 변경.
    form 으로 감싸 입력창에서 [엔터] 입력 시 저장 버튼이 눌리도록 한다."""
    settings = load_settings()
    with st.form("settings_form", border=False):
        show_partial = st.toggle(
            "부분 분석(partial) 안내 표시",
            value=settings.get("show_partial", True),
            help="숨기면 분석 후 파란색 부분 분석 안내 영역이 두 탭 모두에서 표시되지 않습니다.")
        st.divider()
        st.markdown("**비밀번호 설정**")
        st.caption("빈칸으로 두면 기존 비밀번호가 유지됩니다. "
                   "(.env 와 .streamlit/secrets.toml 에 저장)")
        pw_fields = []  # (mode, 라벨, 새 비밀번호, 비밀번호 확인)
        for mode, label in [("current", "현행규정 정합성(분석·초기화)"),
                            ("revision", "제·개정규정 정합성(분석·초기화)"),
                            ("settings", "setting 진입")]:
            c_pw, c_conf = st.columns(2)
            pw = c_pw.text_input(f"{label} 비밀번호", type="password", key=f"set_pw_{mode}")
            conf = c_conf.text_input("비밀번호 확인", type="password",
                                     key=f"set_pw_{mode}_confirm")
            pw_fields.append((mode, label, pw, conf))
        submitted = st.form_submit_button("저장", type="primary", width="stretch")
    if submitted:
        # 새 비밀번호는 확인란과 일치해야만 저장한다(잘못된 입력 방지).
        mismatch = [label for _m, label, pw, conf in pw_fields
                    if (pw or conf) and pw != conf]
        if mismatch:
            st.error("비밀번호와 비밀번호 확인이 일치하지 않습니다: " + ", ".join(mismatch))
        else:
            settings["show_partial"] = show_partial
            save_settings(settings)
            updates = {PW_ENV_KEYS[m]: pw for m, _l, pw, _c in pw_fields if pw}
            if updates:
                update_passwords(updates)
            st.rerun()


# ---------------------------------------------------------------- 대시보드 컴포넌트
# HTML 원본의 CSS/JS를 그대로 사용하고 데이터만 Python 에서 JSON 으로 주입한다.
DASHBOARD_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>
:root{--bg:#f5f7fb;--surface:#fff;--line:#e7eaf0;--text:#1f2937;--muted:#6b7280;--navy:#243b64;--blue:#4777d8;--red:#e45b63;--amber:#e7a52d;--green:#36a269;--shadow:0 7px 24px rgba(34,52,84,.07)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,"Pretendard","Noto Sans KR",Arial,sans-serif;font-size:14px}
.summary{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:26px}.summary-block{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:19px 21px;box-shadow:var(--shadow)}.block-head{display:flex;align-items:center;gap:10px;margin-bottom:16px;font-weight:800;font-size:15px}.dot{width:9px;height:9px;border-radius:50%;background:var(--blue)}.summary-block:nth-child(2) .dot{background:#8b6bd9}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:12px}.metric{border-radius:11px;padding:15px 17px;background:#f8fafc;border:1px solid #edf0f5}.metric-label{color:var(--muted);font-size:12px;margin-bottom:8px}.metric-value{font-size:29px;font-weight:850;letter-spacing:-.05em}.metric-value.red{color:var(--red)}.metric-value.amber{color:var(--amber)}
.toolbar{display:flex;align-items:center;justify-content:space-between;margin:26px 0 12px}.section-title{font-size:17px;font-weight:800}.section-title span{color:var(--muted);font-size:13px;font-weight:500;margin-left:7px}.toolbar-right{display:flex;gap:8px;align-items:center}.select{border:1px solid var(--line);background:#fff;border-radius:8px;padding:9px 12px;color:#475569}.btn{border:1px solid var(--line);background:#fff;border-radius:8px;padding:9px 14px;color:#475569;cursor:pointer;font-weight:700}.btn:hover{filter:brightness(.97)}
.groups{display:flex;flex-direction:column;gap:14px}.group-items{display:flex;flex-direction:column}.group{background:#fff;border:1px solid var(--line);border-radius:13px;overflow:hidden;box-shadow:0 3px 15px rgba(34,52,84,.035)}.group-header{display:flex;align-items:center;justify-content:space-between;padding:17px 20px;background:#fbfcfe;border-bottom:1px solid var(--line)}.group-name{display:flex;align-items:center;gap:10px;font-weight:800}.group-name small{font-size:11px;color:var(--muted);font-weight:600}.count{min-width:27px;height:25px;padding:0 8px;border-radius:20px;background:#eef3ff;color:var(--blue);display:grid;place-items:center;font-size:12px}.count.red{color:var(--red);background:#fff0f0}.count.amber{color:#a87310;background:#fff7e5}
.item{padding:18px 20px;border-bottom:1px solid #f0f2f6}.item:last-child{border-bottom:0}.item.completed{background:#f1f4f7;color:#8b95a3;order:99}.item.completed .issue-title,.item.completed .severity-label{color:#8b95a3}.item.completed .code{background:#e2e7ed;color:#7d8795}.item.completed .detail{opacity:.72}.verify-btn{border:1px solid #cfd6df;background:#fff;color:#536174;border-radius:7px;padding:7px 10px;font-size:12px;font-weight:750;cursor:pointer;white-space:nowrap}.verify-btn:hover{background:#f0f3f6}.item.completed .verify-btn{border-color:#b9c2cc;background:#e3e8ed;color:#687585}.item-actions{display:flex;align-items:center;gap:10px}.item-top{display:grid;grid-template-columns:1fr auto;gap:16px;align-items:start;cursor:pointer}.issue{display:flex;gap:10px;align-items:flex-start}.code{font-family:ui-monospace,monospace;font-size:11px;color:var(--blue);background:#edf3ff;padding:5px 7px;border-radius:5px;white-space:nowrap}.issue-title{font-weight:750;line-height:1.45}.issue-title .sub{display:block;color:var(--muted);font-size:12px;font-weight:500;margin-top:4px}.severity{display:flex;align-items:center;gap:8px;white-space:nowrap}.severity-label{font-size:12px;color:var(--muted)}.scale{display:flex;gap:3px}.scale i{width:8px;height:8px;border-radius:2px;background:#e7eaf0}.scale i.on{background:var(--red)}.scale.amber i.on{background:var(--amber)}.chevron{color:#9aa5b5;margin-left:8px;transition:.2s}.item.open .chevron{transform:rotate(180deg)}
.detail{display:none;margin:16px 0 0 74px;padding:16px 18px;background:#fafbfe;border:1px solid #edf0f5;border-radius:10px}.item.open .detail{display:block}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px 22px}.detail h4{margin:0 0 6px;font-size:12px;color:#64748b}.detail p{margin:0;line-height:1.65;color:#374151}.detail .full{grid-column:1/-1}.recommend{border-left:3px solid var(--blue);padding-left:11px}
.analysis-empty{background:#fff;border:1px dashed #cfd6df;border-radius:13px;text-align:center;padding:40px 20px;color:var(--muted)}.analysis-empty strong{display:block;color:var(--text);font-size:15px;margin-bottom:5px}
@media(max-width:850px){.summary{grid-template-columns:1fr}.toolbar{align-items:flex-start;gap:14px;flex-direction:column}.toolbar-right{width:100%;justify-content:flex-end}.detail{margin-left:0}.detail-grid{grid-template-columns:1fr}.detail .full{grid-column:auto}.item-top{grid-template-columns:1fr}.severity{justify-content:flex-start}}
</style></head><body>
<div id="dashboard-root"></div>
<script>
const MODE=__MODE__;
const DATA=__DATA__;
const EMPTY_MSG=__EMPTY_MSG__;
function stars(n,amber=false){return '<span class="scale '+(amber?'amber':'')+'">'+[1,2,3,4,5].map(i=>`<i class="${i<=n?'on':''}"></i>`).join('')+'</span>'}
function summaryMarkup(){const names=MODE==='current'?['상위법령 ↔ 대학규정','대학규정 ↔ 대학규정']:['상위법령 ↔ 제개정규정','대학규정 ↔ 제개정규정'];const counts=Object.fromEntries(DATA.map(g=>[g.key,g.items.filter(x=>!x.completed).length]));return `<section class="summary"><div class="summary-block"><div class="block-head"><span class="dot"></span>${names[0]}</div><div class="metrics"><div class="metric"><div class="metric-label">정합성 불일치</div><div class="metric-value red">${counts['lr-inc']||0}</div></div><div class="metric"><div class="metric-label">정합성 검토필요</div><div class="metric-value amber">${counts['lr-rev']||0}</div></div></div></div><div class="summary-block"><div class="block-head"><span class="dot"></span>${names[1]}</div><div class="metrics"><div class="metric"><div class="metric-label">정합성 불일치</div><div class="metric-value red">${counts['ir-inc']||0}</div></div><div class="metric"><div class="metric-label">정합성 검토필요</div><div class="metric-value amber">${counts['ir-rev']||0}</div></div></div></div></section>`}
function dashboardMarkup(){const total=DATA.reduce((n,g)=>n+g.items.filter(x=>!x.completed).length,0);return summaryMarkup()+`<div class="toolbar"><div class="section-title">정합성 검토 항목 <span>총 ${total}건</span></div><div class="toolbar-right"><select class="select" id="filter" onchange="render()"><option value="all">전체 분류</option><option value="inc">불일치</option><option value="rev">검토 필요</option></select><button class="btn" onclick="toggleAll(true)">전체 펼치기</button><button class="btn" onclick="toggleAll(false)">전체 접기</button></div></div><div class="groups" id="groups"></div>`}
function itemMarkup(g,x){return `<article class="item ${x.completed?'completed':''}"><div class="item-top" onclick="this.parentElement.classList.toggle('open')"><div class="issue"><span class="code">${x.code}</span><div class="issue-title">${x.title}<span class="sub">${x.sub}</span></div></div><div class="item-actions"><div class="severity"><span class="severity-label">${g.type==='inc'?'불일치':'검토 필요'} ${x.level}/5</span>${stars(x.level,g.type==='rev')}<span class="chevron">⌄</span></div><button class="verify-btn" onclick="event.stopPropagation();verifyItem('${g.key}','${x.code}')">${x.completed?'완료취소':'검증완료'}</button></div></div><div class="detail"><div class="detail-grid"><div><h4>관련 내부 규정</h4><p>${x.regs}</p></div><div><h4>관련 상위법령</h4><p>${x.law}</p></div><div class="full"><h4>${g.type==='inc'?'불일치':'검토 필요'} 내용 및 근거</h4><p>${x.basis}</p></div><div class="full"><h4>수정 권고안</h4><p class="recommend">${x.rec}</p></div></div></div></article>`}
function render(){const root=document.getElementById('dashboard-root');const old=document.getElementById('filter');const filter=old?old.value:'all';root.innerHTML=dashboardMarkup();document.getElementById('filter').value=filter;const groups=document.getElementById('groups');if(!DATA.length||!DATA.some(g=>g.items.length)){groups.innerHTML=`<div class="analysis-empty"><strong>${EMPTY_MSG[0]}</strong>${EMPTY_MSG[1]}</div>`;return}groups.innerHTML=DATA.filter(g=>filter==='all'||g.type===filter).map(g=>{const active=g.items.filter(x=>!x.completed).sort((a,b)=>b.level-a.level),completed=g.items.filter(x=>x.completed).sort((a,b)=>b.level-a.level);return `<section class="group"><div class="group-header"><div class="group-name"><span class="dot" style="background:${g.type==='inc'?'var(--red)':'var(--amber)'}"></span>${g.title}<small>${active.length}건</small></div><span class="count ${g.type==='inc'?'red':'amber'}">${active.length}</span></div><div class="group-items">${[...active,...completed].map(x=>itemMarkup(g,x)).join('')}</div></section>`}).join('')}
function verifyItem(key,code){const item=DATA.find(g=>g.key===key)?.items.find(x=>x.code===code);if(!item)return;item.completed=!item.completed;render()}
function toggleAll(open){document.querySelectorAll('.item').forEach(x=>x.classList.toggle('open',open))}
render();
</script></body></html>
"""


def render_dashboard(mode, data, empty_msg, height=760):
    html = (DASHBOARD_TEMPLATE
            .replace("__MODE__", json.dumps(mode))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__EMPTY_MSG__", json.dumps(empty_msg, ensure_ascii=False)))
    components.html(html, height=height, scrolling=True)


# ---------------------------------------------------------------- 페이지 스타일
st.markdown("""
<style>
.stApp{background:#f5f7fb}
.block-container{max-width:1480px;padding:28px 34px 56px}
header[data-testid="stHeader"]{background:transparent}
/* 탭 스타일: HTML 원본 .tab-btn 과 동일 톤 */
.stTabs [data-baseweb="tab-list"]{gap:6px;border-bottom:1px solid #e7eaf0}
.stTabs [data-baseweb="tab"]{padding:13px 18px;color:#6b7280;font-weight:800;font-size:14px;background:transparent}
.stTabs [aria-selected="true"]{color:#243b64;border-bottom:3px solid #243b64}
.stTabs [data-baseweb="tab-highlight"]{background-color:#243b64}
/* 상단 브랜드 바 */
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.brand{display:flex;gap:13px;align-items:center}
.brand-mark{width:38px;height:38px;border-radius:11px;background:#243b64;color:#fff;display:grid;place-items:center;font-weight:800;font-size:17px;box-shadow:0 5px 13px #243b6435}
.eyebrow{font-size:11px;color:#4777d8;font-weight:800;letter-spacing:.08em;margin-bottom:4px}
.main-title{font-size:23px;font-weight:800;letter-spacing:-.04em;color:#1f2937}
/* 패널 제목 */
.panel-title{font-size:20px;font-weight:850;letter-spacing:-.035em;margin:14px 0 6px;color:#1f2937}
.panel-desc{color:#6b7280;font-size:13px;margin-bottom:14px}
/* 부분 분석(partial) 안내 박스: 최대 4줄 높이, 넘치면 우측 스크롤 (가로 폭은 아래 블록과 동일) */
.warn-box{background:#eef4ff;border:1px solid #d7e3fb;border-radius:10px;padding:12px 16px;color:#31507e;font-size:13px;line-height:1.75;margin-bottom:12px;max-height:calc(1.75em * 4 + 24px);overflow-y:auto;box-sizing:border-box;width:100%}
.warn-box::-webkit-scrollbar{width:8px}
.warn-box::-webkit-scrollbar-thumb{background:#b9cdf0;border-radius:4px}
.warn-box::-webkit-scrollbar-track{background:transparent}
/* 최근 분석일: 분석 실행 버튼 상단 우측 정렬 */
.last-analyzed{font-size:12px;color:#6b7280;text-align:right;margin-bottom:6px;white-space:nowrap}
/* 분석 진행 오버레이: 원형 프로그레스바 + 메시지를 화면 정중앙에 표시 */
.overlay-spinner{position:fixed !important;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;background:rgba(245,247,251,.72);backdrop-filter:blur(2px)}
.overlay-card{background:#fff;border:1px solid #e7eaf0;border-radius:13px;padding:22px 30px;box-shadow:0 12px 40px rgba(34,52,84,.18);display:flex;align-items:center;gap:14px;font-weight:700;color:#243b64;font-size:14px;max-width:70vw}
.overlay-ring{width:26px;height:26px;border:3px solid #dbe4f5;border-top-color:#4777d8;border-radius:50%;animation:ovspin .9s linear infinite;flex:none}
@keyframes ovspin{to{transform:rotate(360deg)}}
/* 기본 st.spinner 도 혹시 쓰이면 중앙 오버레이로 */
div[data-testid="stSpinner"],div.stSpinner{position:fixed !important;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;background:rgba(245,247,251,.72)}
/* setting 버튼: 테두리 없이 글씨만 — 감싸는 컨테이너(우측 정렬)를 흐름에서
   높이 0 으로 만들고 바로 아래 탭 행(구분선 위쪽)의 우측 끝에 겹쳐 배치 */
.st-key-settings_bar{height:0 !important;min-height:0 !important;margin:0;padding:0;overflow:visible !important;position:relative;z-index:5}
.st-key-btn_settings button{border:none !important;background:transparent !important;box-shadow:none !important;color:#6b7280;font-weight:700;font-size:13px;padding:4px 8px;min-height:0;transform:translateY(22px)}
.st-key-btn_settings button:hover{color:#243b64}
.st-key-btn_settings button:focus,.st-key-btn_settings button:active{outline:none;box-shadow:none !important}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------- 상단 브랜드 바
st.markdown("""
<div class="topbar"><div class="brand"><div class="brand-mark">H</div>
<div><div class="eyebrow">REGULATION INSIGHT</div>
<div class="main-title">한양여자대학교 규정 정합성 평가 대시보드</div></div></div></div>
""", unsafe_allow_html=True)

# 두 탭 모두 세션이 아니라 analysis_store.json(영구 저장)에서 결과를 읽는다.

# 클러스터별 통합 규정 텍스트(클러스터명.txt)가 저장된 폴더
CLUSTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regulationsByCluster")

# 주제별 클러스터 (주제별 클러스터.txt / 시스템 프롬프트의 A~K 정의와 동일)
CLUSTER_OPTIONS = [
    "A. 조직·거버넌스·정관",
    "B. 학사제도(교육과정·수업·학적)",
    "C. 등록금·재정·예산·회계",
    "D. 교원 인사",
    "E. 직원 인사",
    "F. 학생 지도·상벌·자치",
    "G. 장학금",
    "H. 연구·산학협력·지식재산",
    "I. 시설·안전·정보보안·기타 운영지원",
    "J. 국제교류·외국인 유학생·부속/부설기관",
    "K. 개인정보·정보보안",
]


def load_cluster_files(cluster_id):
    """regulationsByCluster 폴더에서 클러스터 ID(A~K)로 시작하는 클러스터명.txt 를 모두 찾아
    [(파일명, 본문 텍스트), …] 를 반환한다. 150KB 초과로 분할된 파일
    (예: A1. ….txt, A2. ….txt)은 번호순으로 정렬해 모두 포함한다."""
    if not os.path.isdir(CLUSTER_DIR):
        raise FileNotFoundError(f"클러스터 폴더가 없습니다: {CLUSTER_DIR}")
    pat = re.compile(rf"^{re.escape(cluster_id)}(\d*)\..*\.txt$", re.IGNORECASE)
    found = []
    for name in os.listdir(CLUSTER_DIR):
        m = pat.match(name)
        if m:
            found.append((int(m.group(1) or 0), name))
    if not found:
        raise FileNotFoundError(
            f"regulationsByCluster 폴더에서 '{cluster_id}' 로 시작하는 .txt 파일을 찾지 못했습니다.")
    parts = []
    for _num, name in sorted(found):
        with open(os.path.join(CLUSTER_DIR, name), "r", encoding="utf-8") as fp:
            parts.append((name, fp.read().strip()))
    return parts


def build_current_prompt(cluster_id, file_name, text):
    """현행규정 모드 프롬프트 1건: 분석모드 + 선택클러스터(A~K) + 클러스터명.txt 1개 첨부."""
    return (f"분석모드:현행규정\n선택클러스터:{cluster_id}\n\n"
            f"[첨부파일: {file_name}]\n{text}")


REVISION_MERGED_NAME = "M. 제개정규정통합"


def build_revision_merged_files(files):
    """업로드된 모든 PDF 에서 텍스트를 추출(regulation_parser 와 동일한 방법)해
    regulationsByCluster/M. 제개정규정통합.txt 로 생성한다.
    - 각 규정의 장·조 앞에 (규정명) 접두어를 삽입한다(파일명에서 확장자 제거 = 규정명,
      regulation_parser.parse_rule_text 재사용 — 클러스터 txt 와 동일한 형식).
    - 150KB 를 넘으면 M1, M2 … 로 분할 저장(regulation_parser 의 분할 로직 재사용).
    생성된 파일을 [(파일명, 내용), …] 로 반환한다."""
    blocks = []
    for f in files:
        pdf_bytes = f.getvalue()
        rule_name = os.path.splitext(f.name)[0].strip()  # "교환학생관리 규정.pdf" -> "교환학생관리 규정"
        raw = rp.extract_pdf_text(pdf_bytes)
        parsed = rp.parse_rule_text(raw, rule_name)
        if not parsed:
            # 텍스트 순서가 깨진 PDF 폴백: 글자 좌표 기반 재구성(regulation_parser 와 동일)
            try:
                raw = rp.extract_pdf_text_charsort(pdf_bytes)
                parsed = rp.parse_rule_text(raw, rule_name)
            except Exception:
                parsed = ""
        if not parsed:
            # 본문 시작점(제1장/제1조)을 찾지 못한 경우: 원문 전체에 규정명 블록으로 표기
            parsed = f"({rule_name})\n" + rp.RE_CTRL.sub("", raw).strip()
        blocks.append(parsed)
    rp._write_cluster_split(REVISION_MERGED_NAME, blocks)
    return load_cluster_files("M")


def merge_agent_results(results):
    """
    분할 파일별 API 호출 반환 JSON 들을 하나로 합산한다.
    - issues: 모두 이어 붙인 뒤 issue_id 일련번호를 접두어별로 001부터 재부여
      (호출마다 001부터 시작하므로 합산 시 중복 방지)
    - analysis: 하나라도 partial 이면 partial, reference_date 는 가장 최근 값
    - retrieval_report.warnings: 중복 제거 후 합산
    """
    results = [r for r in results if r]
    if len(results) == 1:
        return results[0]
    merged = {"analysis": {}, "retrieval_report": {"warnings": []}, "issues": []}
    warnings, issues = [], []
    for r in results:
        a = r.get("analysis", {})
        if not merged["analysis"]:
            merged["analysis"] = dict(a)
        if a.get("status") == "partial":
            merged["analysis"]["status"] = "partial"
        if a.get("reference_date", "") > merged["analysis"].get("reference_date", ""):
            merged["analysis"]["reference_date"] = a["reference_date"]
        for w in r.get("retrieval_report", {}).get("warnings") or []:
            if w not in warnings:
                warnings.append(w)
        issues.extend(r.get("issues") or [])
    counters = {}
    for issue in issues:
        m = re.match(r"^([A-Z]{2}-[A-Z]{3})-\d+$", issue.get("issue_id") or "")
        if m:
            key = m.group(1)
            counters[key] = counters.get(key, 0) + 1
            issue["issue_id"] = f"{key}-{counters[key]:03d}"
    merged["retrieval_report"]["warnings"] = warnings
    merged["issues"] = issues
    return merged


# ---------------------------------------------------------------- 초기화 확인 다이얼로그
@st.dialog("초기화 확인")
def confirm_reset(mode, cluster_id=None):
    """초기화 확인 후 삭제. ‘확인’ 시 삭제 + 전체 rerun 되어 분석 버튼이 초기 라벨로 복귀한다."""
    st.warning("모든 데이터가 삭제됩니다. 초기화하시겠습니까?")
    c_ok, c_cancel = st.columns(2)
    if c_ok.button("확인", type="primary", width="stretch", key="confirm_reset_ok"):
        if mode == "current":
            delete_cluster_result(cluster_id)
        else:
            clear_revision_result()
        st.rerun()
    if c_cancel.button("취소", width="stretch", key="confirm_reset_cancel"):
        st.rerun()


# ---------------------------------------------------------------- 탭 2개
# setting 버튼(테두리 없이 글씨만): 탭 구분선 우측 상단에 겹쳐 우측 정렬로 배치
# (CSS .st-key-settings_bar 가 높이 0 처리 후 탭 행 위로 이동시킨다)
with st.container(key="settings_bar", horizontal=True, horizontal_alignment="right"):
    if st.button("setting", key="btn_settings"):
        ask_password("settings", "open")  # 진입 비밀번호(최초 2465) 확인 후 설정창 열기
if st.session_state.pop("pw_verified_settings_open", False):
    settings_dialog()
tab_current, tab_revision = st.tabs(["현행규정 정합성", "제·개정규정 정합성"])

# ------------------------------------------------------------ 탭 1: 현행규정
with tab_current:
    head_l, head_m, head_r = st.columns([3, 1.3, 1.1], vertical_alignment="bottom")
    with head_l:
        st.markdown('<div class="panel-title">현행규정 정합성 평가</div>'
                    '<div class="panel-desc">“상위규정 ↔ 대학규정”, “대학규정 ↔ 대학규정”에 대한 정합성 평가</div>',
                    unsafe_allow_html=True)
    with head_m:
        cluster_current = st.selectbox(
            "주제별 클러스터", CLUSTER_OPTIONS, index=0,  # 초기 선택값: A. 조직·거버넌스·정관
            key="cluster_current", label_visibility="collapsed")
    cluster_id = cluster_current.split(".")[0].strip()  # "K. 개인정보…" -> "K"
    # 1회 이상 분석한 클러스터면 저장된 결과를 읽어와 화면에 맵핑한다.
    stored = load_cluster_result(cluster_id)
    with head_r:
        date_ph = st.empty()  # 최근 분석일 표시 자리(버튼 상단) — 분석 후 최신값으로 채움
        c_run, c_reset = st.columns([1.6, 1])
        run_label = "다시 분석" if stored else "정합성 분석 실행"
        if c_run.button(run_label, type="primary", use_container_width=True, key="run_current"):
            # 비밀번호 확인 다이얼로그 → 일치 시 rerun 후 아래 플래그 분기에서 분석 실행
            ask_password("current", "run")
        if st.session_state.pop("pw_verified_current_run", False):
            try:
                parts = load_cluster_files(cluster_id)
                request_info = [
                    "분석모드: 현행규정",
                    f"선택클러스터: {cluster_id}",
                    f"첨부파일({len(parts)}개 → API {len(parts)}회 호출): "
                    + ", ".join(f"{name} ({len(text):,}자)" for name, text in parts),
                ]
                # 분할 파일 개수만큼 API 를 반복 호출하고 반환 JSON 을 모두 합산한다.
                results = []
                for i, (name, text) in enumerate(parts, 1):
                    with overlay_spinner(f"[{i}/{len(parts)}] {name} 정합성 분석 중… (수십 초 소요)"):
                        prompt = build_current_prompt(cluster_id, name, text)
                        results.append(call_factchat(prompt))
                merged = merge_agent_results(results)
                stored = {
                    "agent_json": merged,
                    "dashboard_data": agent_json_to_dashboard_data(merged, "current"),
                    "request_info": request_info,
                    "analyzed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                save_cluster_result(cluster_id, stored)  # 기존 저장 데이터 덮어쓰기
            except Exception as e:
                st.error(f"분석 실패: {e}")
        if c_reset.button("초기화", use_container_width=True, key="reset_current",
                          disabled=not stored):
            # 분석 버튼과 동일한 비밀번호 확인 → 일치 시 초기화 확인 다이얼로그
            ask_password("current", "reset")
        if st.session_state.pop("pw_verified_current_reset", False):
            confirm_reset("current", cluster_id)
        date_ph.markdown(last_analyzed_html(stored.get("analyzed_at") if stored else None),
                         unsafe_allow_html=True)

    if stored:
        show_request_info(stored.get("request_info"))
        show_warnings(stored.get("agent_json"))
    render_dashboard(
        mode="current",
        data=stored["dashboard_data"] if stored else [],
        empty_msg=["현행규정 정합성 분석을 실행해 주세요.",
                   "우측 상단의 ‘정합성 분석 실행’ 버튼을 누르면 Agent 분석 결과가 표시됩니다."]
        if not stored else
        ["표시할 이슈가 없습니다.", "Agent 분석 결과 불일치/검토필요 항목이 없습니다."],
        height=980,
    )

# ------------------------------------------------------------ 탭 2: 제·개정규정
with tab_revision:
    head_l, head_r = st.columns([3, 2], vertical_alignment="bottom")
    with head_l:
        st.markdown('<div class="panel-title">제·개정규정 정합성 평가</div>'
                    '<div class="panel-desc">“상위규정 ↔ 제개정규정”, “대학규정 ↔ 제개정규정”에 대한 정합성 평가</div>',
                    unsafe_allow_html=True)
    # 마지막으로 저장된 제개정 분석 결과를 읽어와 화면에 맵핑한다(reload 시 복원).
    stored_rev = load_revision_result()
    with head_r:
        files = st.file_uploader(
            "제개정규정 불러오기", type=["pdf"], accept_multiple_files=True,
            key="uploader", label_visibility="collapsed")
        n = len(files) if files else 0
        col_status, col_btn, col_reset = st.columns([2.4, 1.8, 1], vertical_alignment="bottom")
        col_status.caption(f"{n}개 파일 선택됨" if n else "선택된 파일 없음")
        with col_btn:
            date_ph = st.empty()  # 최근 분석일 표시 자리(버튼 상단) — 분석 후 최신값으로 채움
            run_label_rev = "다시 분석" if stored_rev else "정합성 분석 실행"
            if st.button(run_label_rev, type="primary", disabled=not n,
                         use_container_width=True, key="run_revision"):
                # 비밀번호 확인 다이얼로그 → 일치 시 rerun 후 아래 플래그 분기에서 분석 실행
                ask_password("revision", "run")
            if st.session_state.pop("pw_verified_revision_run", False) and n:
                try:
                    # 모든 업로드 PDF 에서 텍스트 추출 후 M. 제개정규정통합.txt 생성
                    # (150KB 초과 시 M1, M2 … 로 분할)
                    with overlay_spinner(f"{n}개 PDF 텍스트 추출 및 통합 파일 생성 중…"):
                        merged_files = build_revision_merged_files(files)
                    file_names = [f.name for f in files]
                    request_info = [
                        "분석모드: 제개정규정",
                        f"업로드 PDF({n}개): " + ", ".join(file_names),
                        f"통합파일({len(merged_files)}개 → API {len(merged_files)}회 호출): "
                        + ", ".join(f"{name} ({len(text):,}자)" for name, text in merged_files),
                    ]
                    # 통합 파일 개수만큼 API 를 반복 호출하고 반환 JSON 을 모두 합산한다.
                    results = []
                    for i, (name, text) in enumerate(merged_files, 1):
                        with overlay_spinner(f"[{i}/{len(merged_files)}] {name} "
                                             f"정합성 분석 중… (수십 초 소요)"):
                            results.append(call_factchat(
                                f"분석모드:제개정규정\n\n[첨부파일: {name}]\n{text}"))
                    merged = merge_agent_results(results)
                    stored_rev = {
                        "files": file_names,  # 마지막 업로드 데이터 이름
                        "agent_json": merged,
                        "dashboard_data": agent_json_to_dashboard_data(merged, "revision"),
                        "request_info": request_info,
                        "analyzed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                    # 업로드 파일명들을 키로 영구 저장(같은 조합 재분석 시 덮어쓰기)
                    save_revision_result("|".join(sorted(file_names)), stored_rev)
                    st.success(f"{n}개 파일 분석 완료")
                except Exception as e:
                    st.error(f"분석 실패: {e}")
        if col_reset.button("초기화", use_container_width=True, key="reset_revision",
                            disabled=not stored_rev):
            # 분석 버튼과 동일한 비밀번호 확인 → 일치 시 초기화 확인 다이얼로그
            ask_password("revision", "reset")
        if st.session_state.pop("pw_verified_revision_reset", False):
            confirm_reset("revision")
        date_ph.markdown(
            last_analyzed_html(stored_rev.get("analyzed_at") if stored_rev else None),
            unsafe_allow_html=True)

    if stored_rev:
        show_request_info(stored_rev.get("request_info"))
        show_warnings(stored_rev.get("agent_json"))
    render_dashboard(
        mode="revision",
        data=stored_rev["dashboard_data"] if stored_rev else [],
        empty_msg=["제·개정규정을 불러와 분석해 주세요.",
                   "여러 파일을 한 번에 선택할 수 있으며, 분석 전 모든 개수는 0으로 표시됩니다."]
        if not stored_rev else
        ["표시할 이슈가 없습니다.", "Agent 분석 결과 불일치/검토필요 항목이 없습니다."],
        height=980,
    )
