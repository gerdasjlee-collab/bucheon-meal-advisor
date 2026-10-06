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


APPS_SCRIPT_CODE = """// Google Apps Script — 시트에 상담 기록·승인처리 한 줄씩 추가
// 1) 구글 시트 새로 만들기 → 메뉴 [확장 프로그램] → [Apps Script]
// 2) 아래 코드 전체 붙여넣기 → 저장
// 3) [배포] → [새 배포] → 유형: 웹 앱 / 실행: 나 / 액세스: 모든 사용자 → 배포
// 4) 생성된 "웹 앱 URL"을 Streamlit Secrets 의 LOG_WEBHOOK_URL 에 넣기
const COLS = ["상담번호","일시","기관명","담당자","직책","연락처","대상","식단유형","상담유형","입력요약","답변요약","감수판정","승인상태","승인자","승인일시","승인의견","모델"];
function doPost(e) {
  const sh = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  if (sh.getLastRow() === 0) sh.appendRow(COLS);
  const d = JSON.parse(e.postData.contents);
  sh.appendRow(COLS.map(k => d[k] || ""));
  return ContentService.createTextOutput("ok");
}
"""
