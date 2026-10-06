# -*- coding: utf-8 -*-
"""상담 기록(로그) 저장·통계 모듈.

저장 순서:
 1) Google Sheets (Apps Script 웹앱 URL이 Secrets의 LOG_WEBHOOK_URL 에 있으면 POST) — 영구 보관
 2) 앱 메모리 백업 (서버 재시작 시 사라짐) — 관리자 탭에서 CSV로 내려받기
"""
import csv
import datetime as _dt
import io
import json
import threading
import urllib.request

import streamlit as st

COLUMNS = [
    "일시", "기관명", "담당자", "직책", "연락처", "대상", "식단유형", "상담유형",
    "입력요약", "답변요약", "감수판정", "모델",
]


def now_kst() -> str:
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")


@st.cache_resource
def _memory_log() -> list:
    return []


def extract_review(text: str) -> str:
    """답변에서 [감수 판정] 줄을 뽑아낸다."""
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
        pass  # 웹훅 실패해도 앱은 계속 동작 (메모리 백업은 남음)


def record(user: dict, target: str, diet_type: str, task: str, user_input: str, answer: str, model: str, webhook_url: str = ""):
    row = {
        "일시": now_kst(),
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
        "모델": model,
    }
    _memory_log().append(row)
    if webhook_url:
        threading.Thread(target=_post_webhook, args=(webhook_url, row), daemon=True).start()
    return row


def all_rows() -> list:
    return list(_memory_log())


def to_csv(rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in COLUMNS})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM: 엑셀에서 한글 깨짐 방지


APPS_SCRIPT_CODE = """// Google Apps Script — 시트에 상담 기록 한 줄씩 추가
// 1) 구글 시트 새로 만들기 → 메뉴 [확장 프로그램] → [Apps Script]
// 2) 아래 코드 전체 붙여넣기 → 저장
// 3) [배포] → [새 배포] → 유형: 웹 앱 / 실행: 나 / 액세스: 모든 사용자 → 배포
// 4) 생성된 "웹 앱 URL"을 Streamlit Secrets 의 LOG_WEBHOOK_URL 에 넣기
const COLS = ["일시","기관명","담당자","직책","연락처","대상","식단유형","상담유형","입력요약","답변요약","감수판정","모델"];
function doPost(e) {
  const sh = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  if (sh.getLastRow() === 0) sh.appendRow(COLS);
  const d = JSON.parse(e.postData.contents);
  sh.appendRow(COLS.map(k => d[k] || ""));
  return ContentService.createTextOutput("ok");
}
"""
