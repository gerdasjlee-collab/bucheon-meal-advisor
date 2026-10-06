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
    for line in text.splitlines():
        if "감수 판정" in line or "감수판정" in line:
            s = line.replace("*", "").replace("#", "").strip()
            if "불필요" in s:
                return "감수 불필요"
            if "필요" in s:
                return "센터 감수 요청 필요"
            return s[:60]
    return "미판정"


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


def approve(rec_id: str, status: str, approver: str, comment: str, webhook_url: str = "") -> dict | None:
    """영양사 승인 처리: 메모리 행 갱신 + 시트에 '승인처리' 행 추가."""
    for r in _memory_log():
        if r["상담번호"] == rec_id:
            r["승인상태"], r["승인자"], r["승인일시"], r["승인의견"] = status, approver, now_str(), comment
            log_row = dict(r)
            log_row["상담유형"] = "✅ 영양사 승인처리"
            log_row["일시"] = r["승인일시"]
            _send(log_row, webhook_url)
            return r
    return None


def all_rows() -> list:
    return list(_memory_log())


def pending_rows() -> list:
    return [r for r in _memory_log() if r["승인상태"] == PENDING]


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
    s.getRange("A4").setValue("승인 대기");              s.getRange("B4").setFormula(`=IFERROR(COUNTA(QUERY(${R},"select A ${C} and M='승인 대기'",1))-1,0)`);
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

function doPost(e) {
  setup();
  const log = ss_().getSheetByName("기록");
  const d = JSON.parse(e.postData.contents);
  log.appendRow(COLS.map(k => d[k] || ""));
  return ContentService.createTextOutput("ok");
}

function doGet() { setup(); return ContentService.createTextOutput("ok - 기록/통계 시트 준비됨"); }
"""
