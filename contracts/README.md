# 계약 (contracts)

3명이 병렬로 구현하기 위한 인터페이스 정의. **이 파일들이 먼저 고정되어야 병렬이 성립합니다.**

```
데이터 레이어 ──[ db_schema.sql ]──▶ 점수 엔진 ──[ score.json ]──▶ LLM ──[ opinion.json ]──▶ UI
```

| 계약 | 생산자 | 소비자 | 파일 |
|---|---|---|---|
| DB 스키마 | 데이터 레이어 | 점수 엔진 | `db_schema.sql` |
| 점수 JSON | 점수 엔진 | LLM · UI | `score.example.json` |
| 의견 JSON | LLM | UI | `opinion.example.json` |

## 왜 이게 먼저인가

UI 담당은 **백엔드가 0줄이어도** `score.example.json`과 `opinion.example.json`만 있으면
화면을 끝까지 만들 수 있습니다. 점수 엔진 담당은 DB가 비어 있어도 `db_schema.sql`에 맞는
가짜 데이터를 넣고 시작할 수 있습니다. 아무도 기다리지 않습니다.

계약을 바꿔야 하면 **먼저 말하고 바꾸세요.** 말없이 바꾸면 남의 작업이 조용히 깨집니다.

## 핵심: 근거 ID

`opinion.json`의 모든 문장에는 `evidence` 배열이 붙습니다. 여기 들어가는 ID는
`score.json`의 지표 ID와 정확히 일치합니다.

```
opinion: { "text": "영업이익률이 업종 중앙값의 두 배입니다.",
           "evidence": ["profitability.operating_margin"] }
                                    ↓ 같은 ID
score:   { "id": "profitability.operating_margin", "percentile": 88, ... }
```

이 연결이 좌측 드릴다운의 전부입니다. **문장 클릭 → evidence ID 조회 → 좌측에 해당 지표 표시.**
이게 없으면 좌측 사이드바는 그냥 숫자 덤프입니다.

## 유효한 근거 ID 목록

| ID | 의미 |
|---|---|
| `score.overall` | 종합 점수 · 업종 내 순위 |
| `profitability.operating_margin` | 영업이익률 |
| `profitability.roe` | ROE |
| `growth.revenue_cagr3` | 매출 3년 CAGR |
| `growth.operating_income_cagr3` | 영업이익 3년 CAGR |
| `stability.debt_to_equity` | 부채비율 |
| `stability.cashflow_quality` | 영업현금흐름 / 순이익 |
| `valuation.per` | PER |
| `valuation.pbr` | PBR |
| `flag.delisting` | 상장폐지 경고 (Form 25) |
| `flag.ma_late` | 이동평균 정배열 지속 경고 |
| `macro.*` | FRED 거시 지표 (`macro.fed_funds` 등) |

여기 없는 ID를 LLM이 만들어내면 UI는 무시하고 넘어갑니다. 화면이 깨지면 안 됩니다.
