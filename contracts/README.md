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

## 지표의 `sources` — 드릴다운이 원본까지 가는 길

각 지표는 **어느 DB 행을 읽었는지** 말합니다. 좌측 패널이 "이 숫자 어디서 나왔어?"에
답할 수 있는 건 이것 때문입니다.

```json
"sources": ["annual_fact:0000320193:2025:operating_income",
            "annual_fact:0000320193:2025:revenue"]
```

형식은 `annual_fact:{cik}:{연도}:{metric}` 과 `price_snapshot:{ticker}:{asof}:{필드}` 둘뿐입니다.

## `status` — 결측을 표현하는 법 (S5)

지표와 축 모두 `status` 를 갖습니다. `"ok"` 아니면 `"insufficient_data"` 입니다.

> **규칙: 있는 것만 평균 낸다. 빠진 건 명시한다. 절반 이상 빠지면 점수를 내지 않는다.**
> 지표 층과 축 층에 똑같이 적용됩니다.

```json
{ "id": "growth", "status": "insufficient_data", "score": null,
  "reason": "지표 절반 이상을 계산할 수 없습니다" }

"score_basis": { "axes_used": 3, "axes_total": 4,
                 "indicators_used": 6, "indicators_total": 8 }
```

`score_basis` 는 **화면에 반드시 표시해야 합니다.** 3축으로 낸 점수와 4축으로 낸 점수는
신뢰도가 다릅니다. 숨기면 거짓말이 됩니다.

실제 예시: [`score.insufficient.example.json`](score.insufficient.example.json) — 신규 상장사,
성장성 축이 통째로 빠진 경우. 손으로 쓴 게 아니라 엔진이 실제로 뽑은 출력입니다.

## 상장폐지 (S6)

| `delisting.status` | 점수 | 화면 |
|---|---|---|
| `"listed"` | 정상 | 평소대로 |
| `"delisting_soon"` | **냅니다** | `flag.delisting` severity `critical` · 최상단 최대 경고 |
| `"delisted"` | **`null`** | 경고 화면만. `axes` 는 빈 배열 |

살 수 없는 주식에 점수를 붙이면 살 수 있는 것처럼 보입니다.
실제 예시: [`score.delisted.example.json`](score.delisted.example.json)

## 분모가 0 이하일 때 — 백분위 0점 고정

적자 기업의 PER은 **음수**입니다. `lower_better` 기준으로 그냥 두면
**가장 싼 주식**이 됩니다. 자본잠식(자본 ≤ 0)이면 부채비율·PBR·ROE가 전부 같은 덫에 걸립니다.

> **규칙: 분모가 0 이하면 백분위를 0으로 고정하고, `value` 는 `null`, `display` 는 `"적자 또는 자본잠식"`.**

한 규칙이 네 경우를 전부 덮습니다. `value` 를 `null` 로 두는 이유는 뒤집힌 숫자가
화면에 새어나가지 않게 하기 위해서입니다.

예외 하나 — **영업현금흐름/순이익**은 적자일 때 `insufficient_data` 입니다.
적자인데 현금이 들어오는 건 *나쁨*이 아니라 *의미 없음*이라서 0점을 주면 틀립니다.

## 모집단은 종목당 하나

지표마다 다른 SIC 레벨을 쓰면 `"동종업계 58개 중 26위"` 라는 문장이 성립하지 않습니다.
8개 지표 중 **5개 이상**이 유효표본 30을 넘는 가장 깊은 레벨 하나를 고르고,
그 레벨에서도 표본이 모자란 개별 지표는 위의 S5 규칙으로 빠집니다.

`peer_group.level` 이 `0` 이면 전체 시장까지 내려간 것입니다. **화면에 구분해서 표시하세요** —
"동종업계 58개"와 "전체 시장 4,800개"는 신뢰도가 다릅니다.
