import datetime as _dt
import html
import re
import zipfile

import streamlit as st
from google import genai
from google.genai import types

import logbook as L
import rules as R

# ─────────────────────────────────────────────
# Page
# ─────────────────────────────────────────────
st.set_page_config(page_title="급식 식단 조정 도우미", page_icon="🍱", layout="wide", initial_sidebar_state="expanded")
st.title("🍱 급식 식단 조정 도우미")
st.caption(f"{R.CENTER_NAME} · 식재료 교체 / 조리법 변경 / 과일·간식 변경 / 식단표 점검")


# ─────────────────────────────────────────────
# Secrets
# ─────────────────────────────────────────────
def _secret(name: str, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:  # noqa: BLE001
        return default


def _secret_int(name: str, default: int) -> int:
    try:
        return int(_secret(name, default))
    except Exception:  # noqa: BLE001
        return default


# ─────────────────────────────────────────────
# Access Gate (시설별 코드 여러 개 허용: "code1,code2")
# ─────────────────────────────────────────────
ACCESS_CODES = [c.strip() for c in str(_secret("ACCESS_CODE", "")).split(",") if c.strip()]
ADMIN_CODES = [c.strip() for c in str(_secret("ADMIN_CODE", "")).split(",") if c.strip()]  # 관리자 여러 명 가능
LOG_WEBHOOK_URL = str(_secret("LOG_WEBHOOK_URL", "")).strip()

if (ACCESS_CODES or ADMIN_CODES) and not st.session_state.get("authed", False):
    st.info("🔒 부천시 어린이집·사회복지시설 전용 앱입니다. 센터에서 안내받은 **접속 코드**와 **상담자 정보**를 입력하세요.")
    with st.form("login"):
        code_in = st.text_input("접속 코드 *", type="password")
        c1, c2 = st.columns(2)
        with c1:
            org_in = st.text_input("기관명 *", placeholder="예: ○○어린이집 / ○○노인요양원")
            name_in = st.text_input("담당자 성명 *", placeholder="예: 홍길동")
        with c2:
            role_in = st.selectbox("직책 *", ["영양사", "조리사", "원장/시설장", "교사/사회복지사", "기타"])
            phone_in = st.text_input("연락처 *", placeholder="예: 032-000-0000")
        agree = st.checkbox("상담 내용(기관명·담당자·질문·답변)이 센터에 기록·보관되며, 식단 변경은 센터 영양사의 승인 후에만 적용할 수 있음을 확인합니다. *")
        ok = st.form_submit_button("입장", use_container_width=True)
    if ok:
        code = code_in.strip()
        is_admin = code in ADMIN_CODES
        if not (is_admin or code in ACCESS_CODES):
            st.error("접속 코드가 올바르지 않습니다. 센터로 문의해 주세요.")
        elif not (org_in.strip() and name_in.strip() and phone_in.strip() and agree):
            st.error("기관명·담당자 성명·연락처 입력과 확인 체크는 필수입니다.")
        else:
            st.session_state.authed = True
            st.session_state.is_admin = is_admin
            st.session_state.user = {"org": org_in.strip(), "name": name_in.strip(), "role": role_in, "phone": phone_in.strip()}
            st.rerun()
    st.stop()

USER = st.session_state.get("user", {"org": "(미입력)", "name": "(미입력)", "role": "", "phone": ""})
IS_ADMIN = bool(st.session_state.get("is_admin", False))

# ─────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown(f"**👤 {USER['org']}** · {USER['name']} {USER['role']}" + (" · 🛡️관리자" if IS_ADMIN else ""))
    if st.button("로그아웃", use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
    st.header("⚙️ 설정")
    default_key = _secret("GEMINI_API_KEY", "")
    if default_key:
        st.success("🔑 센터 API 키 적용됨")
        api_key = default_key
    else:
        api_key = st.text_input("Gemini API Key", type="password")
    model_slot = st.empty()

    st.divider()
    st.subheader("🏫 대상 시설·연령")
    target = st.radio("대상", list(R.TARGETS.keys()), index=1, label_visibility="collapsed")
    diet_type = st.selectbox("식단 유형", R.DIET_TYPES, index=0)
    headcount = st.number_input("조리 기준 인원(명)", min_value=1, max_value=500, value=30)

    with st.expander("📖 센터 규칙 보기"):
        st.markdown("**★ 동일 식품군 내 대체 식재료**")
        st.code(R.SUBSTITUTION_TABLE.strip(), language=None)
        st.markdown("**감수 요청 기준**")
        st.code(R.REVIEW_RULES.strip(), language=None)
        st.markdown("**알레르기 유발물질 19종**")
        st.write(R.ALLERGEN_TEXT)
        st.caption(R.ALLERGEN_NOTE)

    st.divider()
    if st.button("🗑️ 상담 기록 초기화", use_container_width=True):
        for k in ("messages", "last_result", "notice", "menu_text"):
            st.session_state.pop(k, None)
        st.rerun()

if not api_key:
    st.info("👈 사이드바에 Gemini API Key를 입력하면 시작할 수 있습니다.")
    st.stop()

# ─────────────────────────────────────────────
# Gemini client
# ─────────────────────────────────────────────
DEFAULT_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.5-pro"]
EXCLUDE = ("embedding", "tts", "image", "live", "audio", "veo", "imagen", "robotics", "computer")


@st.cache_resource(show_spinner="API 연결 확인 중...")
def get_client(key: str):
    order = [True, False] if key.startswith("AQ.") else [False, True]
    last_err = None
    for use_vertex in order:
        try:
            c = genai.Client(api_key=key, vertexai=use_vertex)
            c.models.generate_content(model=DEFAULT_MODEL, contents="ping")
            return c
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"두 경로 모두 실패: {last_err}")


@st.cache_data(ttl=3600, show_spinner=False)
def list_models(key: str) -> list[str]:
    try:
        c = get_client(key)
        names = sorted({(m.name or "").split("/")[-1] for m in c.models.list()}, reverse=True)
        names = [n for n in names if n.startswith("gemini") and not any(x in n for x in EXCLUDE)]
        pref = [n for n in names if "flash-lite" in n] + [n for n in names if "flash" in n and "lite" not in n] + [n for n in names if "pro" in n]
        return (pref + [n for n in names if n not in pref]) or FALLBACK_MODELS
    except Exception:  # noqa: BLE001
        return FALLBACK_MODELS


try:
    client = get_client(api_key)
except Exception as e:
    st.error(f"API 연결 오류: {e}")
    st.stop()

models = list_models(api_key)
model_name = model_slot.selectbox("모델", models, index=models.index(DEFAULT_MODEL) if DEFAULT_MODEL in models else 0)

SYSTEM_PROMPT = R.build_system_prompt()

# ─────────────────────────────────────────────
# Usage limits
# ─────────────────────────────────────────────
LIMIT_PER_USER = _secret_int("DAILY_LIMIT_PER_USER", 50)
LIMIT_TOTAL = _secret_int("DAILY_LIMIT_TOTAL", 800)


@st.cache_resource
def _global_counter() -> dict:
    return {"date": None, "count": 0}


def _today() -> str:
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y-%m-%d")


def _usage_status():
    today = _today()
    g = _global_counter()
    if g["date"] != today:
        g["date"], g["count"] = today, 0
    if st.session_state.get("usage_date") != today:
        st.session_state.usage_date, st.session_state.usage_count = today, 0
    return st.session_state.usage_count, g["count"]


def _check_and_record() -> bool:
    me, total = _usage_status()
    if me >= LIMIT_PER_USER:
        st.warning(f"⏳ 오늘 상담 한도({LIMIT_PER_USER}회)를 모두 사용했습니다.")
        return False
    if total >= LIMIT_TOTAL:
        st.warning("⏳ 오늘 센터 전체 사용량이 한도에 도달했습니다.")
        return False
    _global_counter()["count"] += 1
    st.session_state.usage_count += 1
    return True


used_me, used_all = _usage_status()
with st.sidebar:
    st.caption(f"오늘 사용: {used_me}/{LIMIT_PER_USER}회 · 전체 {used_all}/{LIMIT_TOTAL}회")


# ─────────────────────────────────────────────
# hwpx → text
# ─────────────────────────────────────────────
def hwpx_to_text(file_bytes) -> str:
    import io

    z = zipfile.ZipFile(io.BytesIO(file_bytes))
    out = []
    for n in sorted(z.namelist()):
        if not n.startswith("Contents/section"):
            continue
        x = z.read(n).decode("utf-8", "ignore")
        x = re.sub(r"</hp:tc>", " | ", x)
        x = re.sub(r"</hp:tr>", "\n", x)
        x = re.sub(r"</hp:p>", " ", x)
        t = html.unescape(re.sub(r"<[^>]+>", "", x))
        t = re.sub(r"(그림|사각형)입니다\.\s*(원본 그림의 이름:.*?세로 \d+pixel)?", "", t, flags=re.S)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n", t)
        out.append(t)
    return "\n".join(out)


# ─────────────────────────────────────────────
# Generation
# ─────────────────────────────────────────────
LEGAL_FOOTER = (
    "\n\n---\n*본 답변은 AI 1차 검토 자료입니다. 식단 변경은 "
    f"**{R.CENTER_NAME} 영양사의 승인 후에만** 적용할 수 있으며, 본 상담은 기관명·담당자와 함께 센터에 기록됩니다.*"
)


def approval_banner(rec_id: str | None):
    msg = "🛑 **센터 영양사 승인 전에는 이 식단 변경을 적용할 수 없습니다.** 센터에서 승인 결과를 안내드립니다."
    if rec_id:
        msg += f"  \n상담번호: `{rec_id}` (문의 시 알려주세요)"
    st.warning(msg)


def context_block() -> str:
    return f"[상담 조건] 기관: {USER['org']} / 담당자: {USER['name']}({USER['role']}) / 대상: {target} / 식단 유형: {diet_type} / 조리 기준 인원: {headcount}명"


def log_it(task: str, user_input: str, answer: str) -> str:
    row = L.record(USER, target, diet_type, task, user_input, answer, model_name, LOG_WEBHOOK_URL)
    return row["상담번호"]


def stream_answer(task_key: str, user_prompt: str, placeholder):
    full = ""
    for ch in client.models.generate_content_stream(
        model=model_name,
        contents=[types.Content(role="user", parts=[types.Part(text=context_block() + "\n\n" + user_prompt)])],
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT + "\n" + R.TASK_PROMPTS[task_key], temperature=0.25),
    ):
        if ch.text:
            full += ch.text
            placeholder.markdown(L.safe_md(full) + "▌")
    placeholder.markdown(L.safe_md(full))
    return full


def make_notice(result_text: str) -> str:
    r = client.models.generate_content(
        model=model_name,
        contents=[types.Content(role="user", parts=[types.Part(text=f"{context_block()}\n\n[상담 결과]\n{result_text}\n\n{R.NOTICE_PROMPT}")])],
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.2),
    )
    return r.text or ""


def run_task(task_key: str, prompt: str):
    if not _check_and_record():
        return
    st.session_state.notice = None
    st.markdown("#### 🧑‍🏫 상담 결과")
    ph = st.empty()
    try:
        text = stream_answer(task_key, prompt, ph)
        ph.markdown(L.safe_md(text) + LEGAL_FOOTER)
        rec_id = log_it(task_key, prompt, text)
        st.session_state.last_result = {"key": task_key, "text": text, "id": rec_id}
        st.session_state.just_ran = True
        approval_banner(rec_id)
    except Exception as e:  # noqa: BLE001
        st.error(f"답변 생성 중 오류: {e}")


def result_and_notice_ui(key: str, with_notice: bool = True):
    res = st.session_state.get("last_result", {})
    if res.get("key") != key or not res.get("text"):
        return
    if not st.session_state.get("just_ran"):  # 버튼 클릭 등으로 화면이 다시 그려질 때 결과 유지
        st.markdown("#### 🧑‍🏫 상담 결과")
        st.markdown(L.safe_md(res["text"]) + LEGAL_FOOTER)
        approval_banner(res.get("id"))
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if with_notice and st.button("📋 조리사용 변경 안내문 만들기", key=f"nb_{key}", use_container_width=True):
            if _check_and_record():
                with st.spinner("안내문 작성 중..."):
                    st.session_state.notice = make_notice(res["text"])
    with c2:
        st.download_button(
            "⬇️ 결과 저장(.md)",
            data=f"# 식단 조정 상담 결과\n\n{context_block()}\n\n{res['text']}",
            file_name=f"식단조정_{_today()}.md",
            mime="text/markdown",
            use_container_width=True,
            key=f"dr_{key}",
        )
    if with_notice and st.session_state.get("notice"):
        st.markdown("#### 📋 조리사용 변경 안내문")
        st.markdown(L.safe_md(st.session_state.notice))
        st.download_button("⬇️ 안내문 저장(.txt)", data=st.session_state.notice, file_name=f"식단변경안내_{_today()}.txt", mime="text/plain", key=f"dn_{key}")


# ─────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────
st.markdown(f"**현재 조건** · {USER['org']} {USER['name']} · {target} · {diet_type} · {headcount}명 기준")
tab_names = list(R.TASK_PROMPTS.keys()) + (["📊 센터 관리"] if IS_ADMIN else [])
tabs = st.tabs(tab_names)
t1, t2, t3, t4, t5 = tabs[:5]
t6 = tabs[5] if IS_ADMIN else None

# ── 식재료 교체 ──
with t1:
    with st.form("f_swap"):
        c1, c2 = st.columns(2)
        with c1:
            menu = st.text_input("메뉴명 (알레르기 번호 포함 가능)", placeholder="예: 쇠고기숙주볶음⑤⑥⑬⑯")
            ingredient = st.text_input("교체할 식재료 *", placeholder="예: 쇠고기")
            group = st.selectbox("식품군 (센터 표 기준)", R.FOOD_GROUP_OPTIONS, index=2)
            method = st.selectbox("현재 조리법", R.COOKING_METHODS, index=3)
        with c2:
            reason = st.multiselect(
                "교체 사유 *",
                ["수급 불가/가격 급등", "알레르기 대응", "수산물 제공 어려움", "식단 중복", "계절·제철 반영", "조리 인력·시간 부족", "선호도 낮음", "기타"],
            )
            constraints = st.text_input("제약 조건", placeholder="예: 돼지고기⑩·달걀① 알레르기 2명, 예산 1인 300원 이내")
            note = st.text_area("추가 정보", placeholder="현재 레시피·분량, 같이 나가는 반찬 등", height=80)
        go = st.form_submit_button("🔄 대체 식재료 추천받기", use_container_width=True)
    if go:
        if not ingredient or not reason:
            st.warning("교체할 식재료와 사유는 필수입니다.")
        else:
            run_task("🔄 식재료 교체", f"메뉴: {menu or '미기재'}\n교체할 식재료: {ingredient} (식품군: {group}, 조리법: {method})\n교체 사유: {', '.join(reason)}\n제약 조건: {constraints or '없음'}\n추가 정보: {note or '없음'}")
    result_and_notice_ui("🔄 식재료 교체")

# ── 조리법 변경 ──
with t2:
    with st.form("f_cook"):
        c1, c2 = st.columns(2)
        with c1:
            menu2 = st.text_input("메뉴명 *", placeholder="예: 고등어구이⑦")
            from_m = st.selectbox("현재 조리법", R.COOKING_METHODS, index=4)
            to_m = st.selectbox("변경할 조리법", R.COOKING_METHODS, index=5)
        with c2:
            reason2 = st.multiselect(
                "변경 사유 *",
                ["조리 설비 부족(오븐·튀김기 없음)", "조리 시간 단축", "기름·나트륨 줄이기(저염저당)", "부드럽게(영아·노인·죽식)", "냄새·연기 문제", "기타"],
            )
            recipe = st.text_area("현재 레시피(재료·분량)", placeholder="예: 고등어 30g×30인분, 소금, 식용유", height=80)
            note2 = st.text_input("추가 정보", placeholder="예: 가스레인지 2구만 사용 가능")
        go2 = st.form_submit_button("🍳 변경 레시피 받기", use_container_width=True)
    if go2:
        if not menu2 or not reason2:
            st.warning("메뉴명과 변경 사유는 필수입니다.")
        else:
            run_task("🍳 조리법 변경", f"메뉴: {menu2}\n조리법 변경: {from_m} → {to_m}\n변경 사유: {', '.join(reason2)}\n현재 레시피: {recipe or '미기재'}\n추가 정보: {note2 or '없음'}")
    result_and_notice_ui("🍳 조리법 변경")

# ── 과일·간식 변경 ──
with t3:
    with st.form("f_snack"):
        c1, c2 = st.columns(2)
        with c1:
            snack = st.text_input("현재 간식 *", placeholder="예: 자른포도 / 꿀설기 / 요플레")
            snack_group = st.selectbox("간식 종류", ["과일", "떡", "빵", "우유·유제품", "채소스틱", "죽", "기타"], index=0)
            month = st.selectbox("제공 월", [f"{m}월" for m in range(1, 13)], index=_dt.date.today().month - 1)
            serving = st.text_input("현재 1인 제공량", placeholder="예: 50g")
        with c2:
            reason3 = st.multiselect(
                "변경 사유 *",
                ["제철 아님/가격 급등", "질식 위험(포도·방울토마토·떡 등)", "알레르기 대응(복숭아⑪·토마토⑫·우유② 등)", "식단 중복", "손질 시간 부족", "저당 필요", "기타"],
            )
            allergy_kids = st.text_input("알레르기 대상자 정보", placeholder="예: 복숭아 1명, 우유 1명")
            budget = st.text_input("단가 조건", placeholder="예: 1인 400원 이내")
        go3 = st.form_submit_button("🍎 대체 간식 추천받기", use_container_width=True)
    if go3:
        if not snack or not reason3:
            st.warning("현재 간식과 변경 사유는 필수입니다.")
        else:
            run_task("🍎 과일·간식 변경", f"현재 간식: {snack} (종류: {snack_group})\n제공 월: {month}\n현재 1인 제공량: {serving or '미기재'}\n변경 사유: {', '.join(reason3)}\n알레르기 대상자: {allergy_kids or '없음'}\n단가 조건: {budget or '없음'}")
    result_and_notice_ui("🍎 과일·간식 변경")

# ── 식단표 점검 ──
with t4:
    st.markdown("센터 식단표 파일(**.hwpx**)을 올리거나, 식단표 내용을 붙여넣으면 알레르기 표기·질식 위험·구성 원칙을 점검합니다.")
    up = st.file_uploader("식단표 파일 업로드 (.hwpx / .txt)", type=["hwpx", "txt"])
    if up is not None:
        try:
            if up.name.lower().endswith(".hwpx"):
                st.session_state.menu_text = hwpx_to_text(up.read())
            else:
                st.session_state.menu_text = up.read().decode("utf-8", "ignore")
            st.success(f"파일에서 {len(st.session_state.menu_text):,}자를 읽었습니다.")
        except Exception as e:  # noqa: BLE001
            st.error(f"파일을 읽지 못했습니다: {e}")
    menu_text = st.text_area("식단표 내용", value=st.session_state.get("menu_text", ""), height=220, placeholder="예) 1일(화) 점심: 수수밥 팽이버섯된장국⑤⑥ 쇠고기숙주볶음⑤⑥⑬⑯ 청포묵무침⑤⑥ 배추김치⑬ ...")
    scope = st.text_input("점검 범위/요청 사항", placeholder="예: 9월 2주차만, 알레르기 번호 누락 위주로")
    if st.button("📋 식단표 점검하기", use_container_width=True):
        if not menu_text.strip():
            st.warning("식단표 내용을 입력하거나 파일을 올려주세요.")
        else:
            body = menu_text.strip()
            if len(body) > 12000:
                body = body[:12000] + "\n...(이하 생략: 범위를 좁혀 다시 점검하세요)"
            run_task("📋 식단표 점검", f"점검 요청: {scope or '전체 점검'}\n\n[식단표]\n{body}")
    result_and_notice_ui("📋 식단표 점검", with_notice=False)

# ── 자유 상담 ──
with t5:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(L.safe_md(m["content"]))
    if q := st.chat_input("급식 관련 질문 (식단·조리·위생·알레르기·원산지·보호자 안내 등)"):
        if _check_and_record():
            st.chat_message("user").markdown(q)
            st.session_state.messages.append({"role": "user", "content": q})
            with st.chat_message("assistant"):
                ph = st.empty()
                try:
                    hist = [types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["content"])]) for m in st.session_state.messages[-20:]]
                    hist[-1] = types.Content(role="user", parts=[types.Part(text=context_block() + "\n\n" + q)])
                    full = ""
                    for ch in client.models.generate_content_stream(
                        model=model_name, contents=hist,
                        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT + "\n" + R.TASK_PROMPTS["💬 자유 상담"], temperature=0.3),
                    ):
                        if ch.text:
                            full += ch.text
                            ph.markdown(L.safe_md(full) + "▌")
                    ph.markdown(L.safe_md(full) + LEGAL_FOOTER)
                    st.session_state.messages.append({"role": "assistant", "content": full})
                    rid = log_it("💬 자유 상담", q, full)
                    approval_banner(rid)
                except Exception as e:  # noqa: BLE001
                    st.error(f"답변 생성 중 오류: {e}")

# ── 센터 관리 (관리자 전용) ──
if IS_ADMIN:
    with t6:
        st.markdown("#### 📊 상담 기록 및 통계 (관리자)")
        rows = L.all_rows()
        if LOG_WEBHOOK_URL:
            st.success("구글 시트 자동 기록이 켜져 있습니다. 아래 표는 현재 서버 세션의 백업 기록입니다.")
        else:
            st.warning("LOG_WEBHOOK_URL 이 설정되지 않아 구글 시트 기록이 꺼져 있습니다. 서버가 재시작되면 아래 기록은 사라지니 주기적으로 CSV를 내려받으세요.")
        # ── 영양사 승인 처리 ──
        st.markdown("### ✅ 영양사 승인 처리")
        pending = L.pending_rows()
        if not pending:
            st.info("승인 대기 중인 상담이 없습니다.")
        else:
            labels = {f"{r['상담번호']} · {r['기관명']} · {r['담당자']} · {r['상담유형']}": r["상담번호"] for r in pending}
            pick = st.selectbox("승인 대기 상담 선택", list(labels.keys()))
            sel = next(r for r in pending if r["상담번호"] == labels[pick])
            with st.expander("상담 내용 보기", expanded=True):
                st.markdown(f"**기관/담당자**: {sel['기관명']} · {sel['담당자']} ({sel['직책']}, {sel['연락처']})  \n**대상**: {sel['대상']} / {sel['식단유형']}  \n**감수 판정**: {sel['감수판정']}")
                st.markdown("**질문 요약**")
                st.code(sel["입력요약"], language=None)
                st.markdown("**AI 답변 요약**")
                st.code(sel["답변요약"], language=None)
            with st.form("approve_form"):
                status = st.radio("승인 결정 *", L.APPROVAL_STATES, horizontal=True)
                comment = st.text_area("영양사 의견 (시설에 전달할 조건·수정 사항)", placeholder="예: 돼지고기 대체 승인. 단, 1.5cm 이하로 썰어 제공하고 원산지 게시판 수정할 것.")
                ok = st.form_submit_button("승인 결정 저장", use_container_width=True)
            if ok:
                L.approve(sel["상담번호"], status, f"{USER['name']}({USER['role']})", comment.strip(), LOG_WEBHOOK_URL)
                st.success(f"{sel['상담번호']} → {status} 처리되었습니다. (승인자: {USER['name']})")
                st.rerun()

        st.markdown("### 📈 통계")
        if not rows:
            st.info("아직 기록이 없습니다.")
        else:
            import collections

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("총 상담 건수", len(rows))
            c2.metric("승인 대기", sum(1 for r in rows if r["승인상태"] == L.PENDING))
            c3.metric("감수 요청 필요", sum(1 for r in rows if "요청" in r["감수판정"]))
            c4.metric("이용 기관 수", len({r["기관명"] for r in rows}))
            st.markdown("**승인 상태별 건수**")
            st.table([{"승인상태": k, "건수": v} for k, v in collections.Counter(r["승인상태"] for r in rows).most_common()])
            st.markdown("**기관별 건수**")
            st.table([{"기관명": k, "건수": v} for k, v in collections.Counter(r["기관명"] for r in rows).most_common()])
            st.markdown("**상담 유형별 건수**")
            st.table([{"상담유형": k, "건수": v} for k, v in collections.Counter(r["상담유형"] for r in rows).most_common()])
            st.markdown("**일별 건수**")
            st.table([{"날짜": k, "건수": v} for k, v in sorted(collections.Counter(r["일시"][:10] for r in rows).items())])
            st.markdown("**감수 요청 필요 건 목록**")
            need = [r for r in rows if "요청" in r["감수판정"]]
            st.dataframe([{k: r[k] for k in ("상담번호", "일시", "기관명", "담당자", "연락처", "상담유형", "승인상태", "입력요약")} for r in need] or [{"안내": "없음"}], use_container_width=True)
            st.markdown("**전체 기록**")
            st.dataframe(rows, use_container_width=True)
            st.download_button("⬇️ 전체 기록 CSV 내려받기 (엑셀용)", data=L.to_csv(rows), file_name=f"상담기록_{_today()}.csv", mime="text/csv")
        with st.expander("🔧 구글 시트 자동 기록 설정 방법 (Apps Script 코드)"):
            st.code(L.APPS_SCRIPT_CODE, language="javascript")

st.session_state.just_ran = False
st.divider()
st.caption("⚠️ 본 앱의 답변은 센터 영양사의 전문적 검토를 전제로 한 참고 자료입니다. 알레르기·질식·감수 관련 최종 판단은 센터 지침과 영양사 확인을 따릅니다.")
