# 작업 분담

3명이 **서로 기다리지 않고** 각자 돌릴 수 있게 쪼갠 것입니다.
가능한 이유는 하나입니다 — `contracts/` 의 계약 3개가 먼저 고정되어 있어서.

## 공통 규칙

- **main에 직접 커밋합니다.** 브랜치·PR 없습니다.
- **계약(`contracts/`)을 바꿔야 하면 먼저 말하세요.** 말없이 바꾸면 남의 작업이 조용히 깨집니다.
- **데이터 파일은 커밋하지 마세요.** `companyfacts.zip` 1.4GB + `submissions.zip` 1.56GB. `.gitignore` 처리돼 있습니다.
- API 키는 `.env`에. 커밋 금지.

---

## 담당 ① — 데이터 레이어

> DB 구축 중인 분. 이미 하던 것 이어서.

**산출물** `etl/` 스크립트 + `data/stocks.db` (커밋 안 함)
**의존** 없음. 지금 바로 시작.
**계약** `contracts/db_schema.sql` 을 그대로 따릅니다.

### 순서

1. **`submissions.zip` (1.56GB) → `company` 테이블**
   - `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`
   - `sic`, `sicDescription`, `tickers`, `exchanges` 추출
   - 필터: `exchanges`에 Nasdaq 또는 NYSE가 있고 `entityType == 'operating'` 인 것만
   - ⚠️ SIC는 `companyfacts.zip`에 **없습니다.** 이 파일에만 있습니다.

2. **`form.idx` → `company.delisted_date`**
   - `https://www.sec.gov/Archives/edgar/full-index/{연도}/QTR{1-4}/form.idx`
   - 폼타입 `25` 필터 (2026 Q2 기준 517건)
   - ⚠️ ETF 클래스 정리가 섞여 있습니다. 회사 상장폐지만 거르세요.

3. **`companyfacts.zip` (1.4GB) → `annual_fact`**
   - metric 7개만: `revenue` `operating_income` `net_income` `assets` `liabilities` `equity` `operating_cashflow`
   - ⚠️ **태그 우선순위 사다리** — TSLA는 Revenue 계열 태그가 17개입니다
   - ⚠️ **`Liabilities` 태그가 없는 회사가 있습니다** (KO 코카콜라). `assets - equity` 폴백 필수
   - ⚠️ 세그먼트별 값이 같은 태그에 섞입니다. 연결 전체 값만

4. **야후 chart API → `price_snapshot`**
   - `https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?range=1y&interval=1d`
   - `User-Agent` 헤더 필요. yfinance 안 써도 됩니다
   - `ma20` `ma60` `ma110` `aligned_days` 계산해서 저장

5. **FRED → `macro`** — `FEDFUNDS`, `CPIAUCSL`, `UNRATE` 정도면 충분

### 완료 기준

```sql
SELECT count(*) FROM company WHERE sic IS NOT NULL;              -- ≥ 4000
SELECT count(DISTINCT cik) FROM annual_fact WHERE metric='revenue'; -- ≥ 4000
SELECT value FROM annual_fact WHERE cik='0000021344' AND metric='liabilities'; -- NOT NULL (KO 폴백 확인)
```
- AAPL · TSLA · KO 세 종목 매출을 실제 10-K와 눈으로 대조

---

## 담당 ② — 점수 엔진 + LLM 오케스트레이터

> 주식 지식과 개발이 둘 다 필요한 구간. 나눌 수 없음.

**산출물** `scoring/` `llm/` + API 엔드포인트 하나
**의존** **없음** — 가짜 DB로 시작합니다. ①을 기다리지 마세요.

### 순서

> ⚠️ [docs/screens.md](docs/screens.md) 의 **S5(데이터 부족) · S6(상장폐지)** 를 먼저 읽으세요.
> 둘 다 `score.json` 계약이 바뀌어야 하는 문제입니다. 나중에 발견하면 다시 짜야 합니다.

1. **가짜 DB 먼저** — `db_schema.sql`대로 만들고 100종목쯤 랜덤 주입. 여기서부터 시작하면 ① 없이 4번까지 갑니다.
2. **지표 8개 계산** — 4축 × 2개
3. **백분위 + SIC 폴백 사다리** — 4자리 → 3자리 → 2자리 → 전체, 유효표본 30 기준
4. **`score.json` 출력** — `contracts/score.example.json` 과 키 구조 일치
5. **LLM tool calling → `opinion.json`** — 문장마다 `evidence` ID 부착. 계약의 ID 목록 밖은 못 쓰게 프롬프트에 박기
6. **`GET /api/opinion?ticker=AAPL`** — 두 JSON을 합쳐서 반환

### 진행 상황 (2026-09-20)

1~4번 완료. `python scoring/test_engine.py` 로 16개 검사 통과.

| 파일 | 내용 |
|---|---|
| `scoring/fake_db.py` | 230종목. 적자·자본잠식·신규상장·시총없음·상폐·폐지예정·표본부족 업종을 일부러 배치 |
| `scoring/engine.py` | 지표 8개 · 백분위 · SIC 폴백 사다리 · S5/S6 처리 |
| `scoring/test_engine.py` | 프레임워크 없는 assert 검사 16개 |

**남은 것: 5번 LLM 오케스트레이터, 6번 API 엔드포인트.**

구현하면서 계약에 실제로 추가된 것 (담당 ③는 다시 받아가세요):
- `fiscal_year` — 재무가 몇 년도 기준인지
- 지표별 `sources` — 드릴다운이 원본 DB 행까지 가는 길
- `status` / `reason` / `score_basis` — 결측 표현 (S5)
- `delisting.status` — `listed` / `delisting_soon` / `delisted` (S6)
- `contracts/score.insufficient.example.json`, `contracts/score.delisted.example.json` — 망가진 케이스 실제 출력

### 완료 기준

- **결정성**: 같은 입력 두 번 → 점수 완전히 동일
- **적자 처리**: 순이익 음수 종목 → `valuation.per`, `profitability.roe` 백분위가 `0` (결측 아님)
- **역방향**: 부채비율·PER·PBR은 값이 낮을수록 백분위가 높음
- **계약 준수**: 출력이 `contracts/*.example.json` 과 같은 키 구조 (검증 스크립트 통과)
- LLM이 계약에 없는 evidence ID를 만들어내지 않음

---

## 담당 ③ — 웹 UI + 해설 검수

> 개발·주식 지식 없는 분. 잡일이 아니라 **타겟 사용자 본인**이라서 맡는 겁니다.

**산출물** `web/index.html` + css/js
**의존** **없음** — `contracts/*.example.json` 만 읽으면 백엔드 0줄로 화면이 완성됩니다.

> ### ⚠️ 만들 화면은 **하나뿐입니다**
> 지금 설계가 끝난 건 **종합의견 결과 화면(S1)** 하나입니다.
> 초기 화면, 로딩, 에러, 상장폐지 종목 화면 등은 **아직 안 정해졌습니다.**
> **상상해서 만들지 마세요.** [docs/screens.md](docs/screens.md) 를 먼저 읽고,
> 미설계 화면은 만드는 대신 **초안을 제안하세요.**

### 순서

1. `contracts/score.example.json`, `contracts/opinion.example.json` 을 `fetch()` 로 읽기
2. **3분할 레이아웃** — 좌: 근거 / 중: 점수+의견 / 우: 대화
3. 가운데: 큰 점수 + `rank_text` + 축 4개 + 의견 문장
4. **★ 문장 클릭 → `evidence` ID → 좌측에 해당 지표 표시** ← 이게 제품의 핵심
5. 좌측: 지표 값 · 업종 중앙값 · `peer_deciles` 10칸 막대 (내 위치 표시)
6. 상단: `flags` 배지 (`flag.ma_late` 등)
7. 용어에 밑줄 → 호버하면 `glossary`의 쉬운 설명
8. 우측: 입력창 + 메시지 목록 (응답은 처음엔 가짜)

### 계속 하는 일 — 해설 검수 (이게 더 중요합니다)

제품의 절반이 "PER이 34.8배입니다. PER이란…" 같은 문장입니다.
**이 문장이 통하는지 판정할 수 있는 사람이 팀에 한 명뿐입니다.**

- `opinion.example.json` 의 문장을 전부 읽고 **모르는 단어에 표시**
- 용어집에 없으면 추가 요청, 설명이 어려우면 다시 써달라고 요청
- 주식 아는 사람 셋이 만들면 초보자가 못 읽는 화면이 나오고, 만든 사람들은 끝까지 모릅니다

### 완료 기준

- 백엔드 없이 `index.html` 만 열어도 화면이 다 나옴
- 문장을 클릭하면 좌측이 그 근거로 바뀜
- 폰 너비에서 안 깨짐
- 계약에 없는 evidence ID가 와도 화면이 안 깨짐 (무시하고 넘어감)

### 미설계 화면

S2~S7(초기화면 · 로딩 · 에러 · 데이터부족 · 상장폐지 · 꼬리질문)은 다음 기획 세션에서
한 번에 정합니다. 그때까지 비워두세요. 특히 **S2 초기 화면은 당신이 초안을 내는 게 맞습니다** —
주식 모르는 사람이 처음 열었을 때 뭐가 보여야 안 막히는지 판단할 수 있는 사람이 팀에 한 명뿐입니다.

### 막히면

**4번 인터랙션 배선이 제일 어렵습니다.** 여기서 이틀 이상 막히면 담당 ②에게 넘기고
정적 레이아웃·스타일·용어집 검수에 집중하세요. UI는 목 데이터로 돌아가니
**크리티컬 패스가 아닙니다.** 여기서 막혀도 전체는 안 밀립니다.

---

## 통합 순서

```
1주차   ① 실데이터        ② 가짜DB로 점수엔진      ③ example.json으로 화면
           ↓                    ↓                        ↓
2주차   ①──────붙임──────②                          ③ 계속
                    ↓
3주차          ②──────붙임──────③   (fetch 경로만 교체)
```

- **① ↔ ② 를 먼저 붙입니다.** 이게 붙어야 제품이 성립합니다.
- ③은 마지막에 `fetch('contracts/score.example.json')` → `fetch('/api/opinion?ticker=...')` 한 줄 교체.
