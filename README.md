# 🍱 급식 식단 조정 도우미 (부천시어린이·사회복지급식관리지원센터)

식재료 교체 / 조리법 변경 / 과일·간식 변경 / 식단표 점검 / 자유 상담 — Streamlit + Gemini
모든 상담은 기관명·담당자와 함께 기록되며, 식단 변경 최종 확정은 센터 영양사 감수를 거칩니다.

## Secrets (Streamlit Cloud → App settings → Secrets)
```toml
GEMINI_API_KEY = "..."
ACCESS_CODE = "시설코드1,시설코드2"      # 쉼표로 여러 개 가능
ADMIN_CODE = "센터관리자코드"            # 📊 센터 관리 탭(통계·CSV) 접근
LOG_WEBHOOK_URL = ""                    # 구글 시트 Apps Script 웹앱 URL (설정 시 자동 기록)
DAILY_LIMIT_PER_USER = 50
DAILY_LIMIT_TOTAL = 800
```
## 구글 시트 자동 기록 설정
앱의 📊 센터 관리 탭 → "구글 시트 자동 기록 설정 방법"의 Apps Script 코드를 시트에 배포하고, 웹앱 URL을 `LOG_WEBHOOK_URL`에 넣으세요.
