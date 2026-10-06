# -*- coding: utf-8 -*-
"""상담 기록(로그) 저장·승인·통계 모듈.

저장 순서:
 1) Google Sheets (Apps Script 웹앱 URL이 Secrets의 LOG_WEBHOOK_URL 에 있으면 POST) — 영구 보관
 2) 앱 메모리 백업 (서버 재시작 시 사라짐) — 관리자 탭에서 CSV로 내려받기
승인: 센터 영양사가 관리자 탭에서 승인/반려/보완요청을 기록하면 같은 상담번호로 '승인처리' 행이 추가된다.
"""
import csv
import datetime as _dt
import io
import json
import threading
import urllib.request

import streamlit as st

COLUMNS = [
    "상담번호", "일시", "기관명", "담당자", "직책", "연락처", "대상", "식단유형", "상담유형",
    "입력요약", "답변요약", "감수판정", "승인상태", "승인자", "승인일시", "승인의견", "모델",
]
APPROVAL_STATES = ["승인", "반려", "보완요청"]
SHEET_URL = "https://docs.google.com/spreadsheets/d/1k9KotxrhsdCAoKikDTLN0AZToxI7fAGzgz3rR34fpU4/edit"
PENDING = "승인 대기"


def now_kst() -> _dt.datetime:
    return _dt.datetime.utcnow() + _dt.timedelta(hours=9)


def now_str() -> str:
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")


@st.cache_resource
def _memory_log() -> list:
    return []


@st.cache_resource
def _seq() -> dict:
    return {"n": 0}


def new_id() -> str:
    _seq()["n"] += 1
    return now_kst().strftime("%Y%m%d-%H%M%S") + f"-{_seq()['n']:03d}"


def safe_md(text: str) -> str:
    """AI 답변을 화면에 그릴 때 마크다운 오작동(물결표 취소선 등) 방지."""
    return text.replace("~", "\\~")


def extract_review(text: str) -> str:
    """답변의 [감수 판정] 표시 이후 내용(줄바꿈 포함)에서 판정을 뽑는다."""
    key = "감수 판정" if "감수 판정" in text else ("감수판정" if "감수판정" in text else None)
    if not key:
        return "미판정"
    tail = text[text.rfind(key) + len(key):][:200].replace("*", "").replace("#", "").replace("]", "").strip()
    if "불필요" in tail:
        return "감수 불필요"
    if "필요" in tail:
        return "센터 감수 요청 필요"
    return (tail.splitlines() or ["미판정"])[0][:60] or "미판정"


def _post_webhook(url: str, row: dict):
    try:
        data = json.dumps(row, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass


def _send(row: dict, webhook_url: str):
    if webhook_url:
        threading.Thread(target=_post_webhook, args=(webhook_url, row), daemon=True).start()


def record(user: dict, target: str, diet_type: str, task: str, user_input: str, answer: str, model: str, webhook_url: str = "") -> dict:
    row = {
        "상담번호": new_id(),
        "일시": now_str(),
        "기관명": user.get("org", ""),
        "담당자": user.get("name", ""),
        "직책": user.get("role", ""),
        "연락처": user.get("phone", ""),
        "대상": target,
        "식단유형": diet_type,
        "상담유형": task,
        "입력요약": user_input.replace("\n", " / ")[:500],
        "답변요약": answer.replace("\n", " ")[:400],
        "감수판정": extract_review(answer),
        "승인상태": PENDING,
        "승인자": "",
        "승인일시": "",
        "승인의견": "",
        "모델": model,
    }
    _memory_log().append(row)
    _send(row, webhook_url)
    return row


APPROVE_MARK = "✅ 영양사 승인처리"


def approve(rec_id: str, status: str, approver: str, comment: str, webhook_url: str = "", base: dict | None = None) -> dict | None:
    """영양사 승인 처리: 시트의 원 상담 행 갱신 + '승인처리' 행 추가 (+ 메모리 백업 갱신)."""
    when = now_str()
    r = None
    for m in _memory_log():
        if m["상담번호"] == rec_id:
            m["승인상태"], m["승인자"], m["승인일시"], m["승인의견"] = status, approver, when, comment
            r = m
            break
    if r is None and base is not None:
        r = dict(base)
        r.update({"승인상태": status, "승인자": approver, "승인일시": when, "승인의견": comment})
    if r is None:
        return None
    if webhook_url:
        log_row = dict(r)
        log_row.update({"action": "approve", "상담유형": APPROVE_MARK, "일시": when})
        _post_webhook(webhook_url, log_row)  # 동기 전송: 바로 새로고침해도 시트에 반영되도록
        sheet_rows.clear()
    return r


def all_rows() -> list:
    return list(_memory_log())


def pending_rows() -> list:
    return [r for r in _memory_log() if r["승인상태"] == PENDING]


@st.cache_data(ttl=60, show_spinner=False)
def sheet_rows(webhook_url: str) -> list:
    """구글 시트 '기록'의 상담 행(승인처리 행 제외)을 가져온다. 실패 시 빈 리스트."""
    if not webhook_url:
        return []
    try:
        url = webhook_url + ("&" if "?" in webhook_url else "?") + "action=rows"
        raw = urllib.request.urlopen(url, timeout=15).read().decode("utf-8")
        data = json.loads(raw)
        return [r for r in data if r.get("상담번호") and r.get("상담유형") != APPROVE_MARK]
    except Exception:  # noqa: BLE001
        return []


def sheet_pending(webhook_url: str) -> list:
    return [r for r in sheet_rows(webhook_url) if (r.get("승인상태") or PENDING) == PENDING]


def to_csv(rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in COLUMNS})
    return ("﻿" + buf.getvalue()).encode("utf-8")


APPS_SCRIPT_CODE = """// Google Apps Script — 상담 기록 저장 + 통계 시트 자동 생성
// 1) 구글 시트 새로 만들기 → 메뉴 [확장 프로그램] → [Apps Script]
// 2) 아래 코드 전체 붙여넣기 → 저장(💾)
// 3) [배포] → [새 배포] → 유형 선택(⚙️): 웹 앱 / 실행 계정: 나 / 액세스 권한: 모든 사용자 → 배포 → 권한 승인
// 4) 생성된 "웹 앱 URL"을 Streamlit Secrets 의 LOG_WEBHOOK_URL 에 넣기
const SHEET_ID = "1k9KotxrhsdCAoKikDTLN0AZToxI7fAGzgz3rR34fpU4"; // 부천센터_식단조정상담기록 시트
function ss_() { try { return SpreadsheetApp.openById(SHEET_ID); } catch (e) { return SpreadsheetApp.getActiveSpreadsheet(); } }
const COLS = ["상담번호","일시","기관명","담당자","직책","연락처","대상","식단유형","상담유형","입력요약","답변요약","감수판정","승인상태","승인자","승인일시","승인의견","모델"];

function setup() {  // 처음 한 번 실행해도 되고, 첫 기록이 들어올 때 자동 실행됨
  const ss = ss_();
  let log = ss.getSheetByName("기록");
  if (!log) { log = ss.getSheets()[0]; log.setName("기록"); }
  if (log.getLastRow() === 0) { log.appendRow(COLS); log.setFrozenRows(1); log.getRange(1,1,1,COLS.length).setFontWeight("bold"); }
  if (!ss.getSheetByName("통계")) {
    const s = ss.insertSheet("통계");
    const R = "기록!A:Q";
    const C = "where A is not null and I <> '✅ 영양사 승인처리'";
    s.getRange("A1").setValue("📊 급식 식단 조정 도우미 — 상담 통계 (자동 갱신)").setFontWeight("bold").setFontSize(13);
    s.getRange("A3").setValue("총 상담 건수");           s.getRange("B3").setFormula(`=IFERROR(COUNTA(QUERY(${R},"select A ${C}",1))-1,0)`);
    s.getRange("A4").setValue("승인 대기");              s.getRange("B4").setFormula(`=IFERROR(B3-(COUNTA(UNIQUE(QUERY(${R},"select A where A is not null and I = '✅ 영양사 승인처리'",1)))-1),B3)`);
    s.getRange("A5").setValue("감수 요청 필요");         s.getRange("B5").setFormula(`=IFERROR(COUNTA(QUERY(${R},"select A ${C} and L contains '요청'",1))-1,0)`);
    s.getRange("A6").setValue("이용 기관 수");           s.getRange("B6").setFormula(`=IFERROR(COUNTA(UNIQUE(QUERY(${R},"select C ${C}",1)))-1,0)`);
    s.getRange("A8").setValue("■ 기관별 건수").setFontWeight("bold");
    s.getRange("A9").setFormula(`=IFERROR(QUERY(${R},"select C, count(A) ${C} group by C order by count(A) desc label C '기관명', count(A) '건수'",1),"")`);
    s.getRange("D8").setValue("■ 상담 유형별 건수").setFontWeight("bold");
    s.getRange("D9").setFormula(`=IFERROR(QUERY(${R},"select I, count(A) ${C} group by I order by count(A) desc label I '상담유형', count(A) '건수'",1),"")`);
    s.getRange("G8").setValue("■ 월별 건수").setFontWeight("bold");
    s.getRange("G9").setFormula(`=IFERROR(QUERY(ARRAYFORMULA({LEFT(기록!B:B,7), 기록!A:A, 기록!I:I}),"select Col1, count(Col2) where Col2 is not null and Col1 <> '일시' and Col3 <> '✅ 영양사 승인처리' group by Col1 order by Col1 label Col1 '월', count(Col2) '건수'",0),"")`);
    s.getRange("J8").setValue("■ 승인 처리 현황").setFontWeight("bold");
    s.getRange("J9").setFormula(`=IFERROR(QUERY(${R},"select M, count(A) where A is not null and I = '✅ 영양사 승인처리' group by M label M '승인상태', count(A) '건수'",1),"")`);
    s.getRange("M8").setValue("■ 대상별 건수").setFontWeight("bold");
    s.getRange("M9").setFormula(`=IFERROR(QUERY(${R},"select G, count(A) ${C} group by G order by count(A) desc label G '대상', count(A) '건수'",1),"")`);
    s.getRange("A20").setValue("■ 감수 요청 필요 건 목록").setFontWeight("bold");
    s.getRange("A21").setFormula(`=IFERROR(QUERY(${R},"select A, B, C, D, F, I, M ${C} and L contains '요청' order by B desc label A '상담번호', B '일시', C '기관명', D '담당자', F '연락처', I '상담유형', M '승인상태'",1),"")`);
    s.setColumnWidths(1, 16, 150);
  }
}

function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function rows_() {  // 기록 시트 전체를 객체 배열로
  const log = ss_().getSheetByName("기록");
  const v = log.getDataRange().getValues();
  const out = [];
  for (let i = 1; i < v.length; i++) {
    if (!v[i][0]) continue;
    const o = {}; COLS.forEach((k, j) => { const x = v[i][j]; o[k] = (x instanceof Date) ? Utilities.formatDate(x, "Asia/Seoul", "yyyy-MM-dd HH:mm:ss") : String(x == null ? "" : x); });
    out.push(o);
  }
  return out;
}

function doPost(e) {
  setup();
  const log = ss_().getSheetByName("기록");
  const d = JSON.parse(e.postData.contents);
  if (d.action === "approve") {   // 앱 관리자(영양사) 승인 → 원 상담 행의 승인상태·승인자·승인일시·승인의견 갱신
    const v = log.getDataRange().getValues();
    for (let i = 1; i < v.length; i++) {
      if (String(v[i][0]) === String(d["상담번호"]) && v[i][8] !== "✅ 영양사 승인처리") {
        log.getRange(i + 1, 13, 1, 4).setValues([[d["승인상태"] || "", d["승인자"] || "", d["승인일시"] || "", d["승인의견"] || ""]]);
      }
    }
  }
  log.appendRow(COLS.map(k => d[k] || ""));   // 이력 행 추가 (상담 또는 승인처리)
  return ContentService.createTextOutput("ok");
}

function doGet(e) {
  setup();
  const a = (e && e.parameter && e.parameter.action) || "";
  if (a === "rows") return json_(rows_());
  return ContentService.createTextOutput("ok - 기록/통계 시트 준비됨");
}
"""
