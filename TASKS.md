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
>
> ### 📦 [팀에 보내주실 것 → docs/handoff-checklist.md](docs/handoff-checklist.md)
> 파일 2개(`stocks.db` 8MB + `delisted.csv`)면 됩니다. 3.2GB DuckDB 는 안 보내셔도 됩니다.

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


### 어디에 저장하나

```
data/                      <- 통째로 .gitignore. 절대 커밋되지 않습니다
├── stocks.db              <- 담당 ① 의 산출물. 파일만 있으면 자동으로 이게 쓰입니다
├── fake.db                <- 가짜 DB (scoring/fake_db.py 가 생성)
└── raw/                   <- 내려받은 원본. ETL 끝나면 지워도 됩니다
    ├── companyfacts.zip   (1.4GB)
    └── submissions.zip    (1.56GB)
```

**`data/stocks.db` 로만 만들어 주세요.** 그 이름이면 서버·엔진·의견 생성이
코드 한 줄 안 고치고 바로 붙습니다. `default_db()` 가 stocks.db 를 먼저 찾고
없으면 fake.db 로 떨어집니다. 실DB를 두고 가짜로 돌려보고 싶으면
`STOCKS_DB=data/fake.db` 로 덮어쓰면 됩니다.

서버를 띄우면 어느 쪽을 쓰는지 찍힙니다:

```
DB : stocks.db (실데이터)
```

**크기 걱정은 안 하셔도 됩니다.** 원본 zip 이 3GB 지만 우리가 쓰는 건
metric 7개 × 연간치뿐입니다. 가짜 DB 가 296종목 4년치에 776KB 이니,
5,800종목 10년치면 **50MB 안팎**입니다. 원본 zip 은 ETL 후 지워도 됩니다.

### DB 연결은 따로 할 게 없습니다

SQLite 는 서버가 아니라 파일입니다. 호스트·포트·계정·비밀번호가 없습니다.
`data/stocks.db` 에 파일을 놓으면 그게 연결의 전부입니다.

```
default_db()          stocks.db 있으면 그거, 없으면 fake.db
     v
sqlite3.connect()     파일 열기
     v
_load()               전부 메모리로 올리고 연결을 닫습니다
```

**엔진은 적재 후 연결을 닫습니다.** 이후 요청은 DB 를 건드리지 않습니다. 그래서:

- 서버를 켜둔 채로 DB 를 다시 만들어도 됩니다 (Windows 에서도 파일이 안 잠깁니다).
  서버는 재시작 전까지 옛 데이터를 그대로 서빙합니다.
- **DB 를 갱신했으면 서버를 재시작해야 반영됩니다.** 자동 감지 안 합니다.

ETL 도중인 DB 를 열면 무슨 일인지 알려주고 멈춥니다:

```
DB에 테이블이 없습니다: data/stocks.db
  없는 테이블 : ['annual_fact', 'company', 'macro', 'price_snapshot']
  ETL이 아직 안 끝났거나 contracts/db_schema.sql 로 만들어지지 않은 파일입니다
```

실측 (296종목 1.8MB / 54ms) 기준 **5,800종목이면 적재 1초, 메모리 36MB** 입니다.

### 커밋하는 것 / 안 하는 것

| | 어디로 |
|---|---|
| `etl/` 스크립트 | **git 에 커밋합니다.** 이게 진짜 산출물입니다 |
| `data/stocks.db` | 커밋 안 함. 아래 방법으로 전달 |
| `data/raw/*.zip` | 커밋 안 함. ETL 후 삭제 |

`git add data/stocks.db` 를 치면 git 이 거부하고 이유를 알려줍니다:

```
The following paths are ignored by one of your .gitignore files:
data
hint: Use -f if you really want to add them.
```

**`-f` 는 쓰지 마세요.** GitHub 은 100MB 넘는 파일을 아예 거부하고 50MB 부터 경고합니다.
원본 zip(1.4GB)은 시도조차 실패합니다. 더 나쁜 건, 큰 파일이 한 번 git 히스토리에
들어가면 나중에 지워도 히스토리에 남아서 저장소가 영구히 무거워진다는 점입니다.

### DB 를 팀에 어떻게 넘기나

**1. 각자 ETL 을 돌리는 게 기본입니다.** `etl/` 스크립트가 커밋돼 있으면
다른 사람이 `python etl/build.py` 한 번으로 같은 DB 를 만듭니다.
재현 가능하고 파일을 주고받을 필요가 없습니다. 이게 `etl/` 을 커밋하는 이유입니다.

**2. 수집에 몇 시간 걸려서 매번 돌리기 싫다면** — 50MB 짜리 `stocks.db` 하나를
드라이브·디스코드로 넘기거나 GitHub Release 에 첨부하세요
(Release 첨부는 2GB 까지 되고 git 히스토리를 더럽히지 않습니다).
받은 사람은 `data/stocks.db` 에 놓기만 하면 됩니다.

### DuckDB 를 쓰고 있다면 — DB 를 바꾸지 마세요

분석용 저장소와 서빙용 저장소는 역할이 다릅니다.

| | 분석용 (DuckDB) | 서빙용 (`data/stocks.db`) |
|---|---|---|
| 하는 일 | 78M행 스캔, 지표 실험 | 메모리에 올려 0.2ms 응답 |
| 크기 | GB 단위 | 수십 MB |
| 의존성 | duckdb | 없음 (stdlib) |

엔진은 DuckDB 의 강점을 하나도 안 씁니다 — 시작할 때 한 번 읽고 끝입니다.
필요한 건 **내보내기 한 단계**뿐입니다.

```bash
python etl/export_to_contract.py --from-zip 2026q1.zip --out data/stocks.db
python scoring/check_db.py data/stocks.db
```

`etl/export_to_contract.py` 는 참고 구현입니다. 실제 SEC 2026q1 로 돌려서
4,244종목 / 68,115행이 나오는 것까지 확인했습니다. DuckDB 를 쓴다면
`_read_zip()` 을 SELECT 로 바꾸면 됩니다 — 나머지는 그대로입니다.

**변환에서 조용히 틀리는 세 곳** (전부 실데이터로 확인):

- **`qtrs`** — `0` 은 시점 값(자산·부채·자본), `4` 는 연간 기간 값(매출·이익·현금흐름).
  헷갈리면 분기 숫자가 연간으로 들어갑니다. 에러가 안 나서 제일 위험합니다.
- **`segments` / `coreg`** — 비어 있는 행만. 아니면 사업부문별 값이 섞입니다.
- **`cik` 패딩** — SEC 원본은 `'6955'`, 계약은 `'0000006955'`.
  안 맞추면 계약의 `sources` 필드가 깨져 좌측 드릴다운이 원본을 못 찾습니다.

그리고 **`liabilities` 폴백은 예외가 아니라 기본**입니다. 2026q1 실측으로
4,244개 중 **1,130개(27%)** 가 부채 태그 없이 `assets - equity` 폴백이 필요했습니다.

### 넘기기 전에 / 받은 뒤에 반드시

```bash
python scoring/check_db.py data/stocks.db
```

계약을 실제로 지켰는지 봅니다. "붙여보고 안 되네" 를 "넘기기 전에 알았네" 로
바꾸는 게 목적입니다. FAIL 이면 엔진이 못 씁니다 (exit 1). WARN 은 돌아가지만
화면이 이상해집니다.

무엇을 보는가:
- 테이블·컬럼·PRIMARY KEY (PK 가 없으면 세그먼트 값이 중복으로 쌓여도 안 걸립니다)
- metric 이름 7개 (계약 밖 이름이 섞이면 엔진이 조용히 무시합니다)
- 종목 수 · SIC 커버리지 · 연도 범위 (3년 CAGR 에는 4개 연도가 필요)
- **Liabilities 폴백** — 자본은 있는데 부채가 없는 종목을 세서 알려줍니다
- 그리고 **실제로 엔진을 돌려서** 몇 종목에 점수가 나오는지, 비교군이
  4자리로 잡히는 비율이 얼마인지 보여줍니다

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

5~6번도 완료. `python llm/test_opinion.py` 로 10개 검사 통과.

| 파일 | 내용 |
|---|---|
| `llm/opinion.py` | 의견 생성. 구조화 출력 + 근거 ID 위조 방어. 키 없으면 목 모드 |
| `api/server.py` | stdlib 서버. `/api/score`(즉시) + `/api/opinion`(SSE) |
| `llm/test_opinion.py` | 검사 10개 |

**tool calling을 안 씁니다.** TASKS.md 원안에 "tool calling"이라고 썼지만,
LLM이 필요로 하는 데이터(점수·거시·가격)는 매번 전부 필요합니다. 탐색할 게 없으니
LLM이 무엇을 가져올지 결정할 이유가 없습니다. 셋을 미리 넣고 호출 한 번으로 끝냅니다.
S7 꼬리질문에서 다른 종목을 묻게 되면 그때 툴이 필요해집니다.

LLM은 **OpenAI `gpt-5.6-luna`** (Responses API · 구조화 출력 · 스트리밍).
키는 `.env` 의 `OPENAI_API_KEY`. 모델은 `OPENAI_MODEL` 로 덮어쓸 수 있습니다.

**✅ 실제 호출 확인 완료 (2026-09-20)** — 18초, 근거 위조 0건, 용어 해설 한국어 양호.
1건당 약 3.2원.

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

> **[docs/integration.md](docs/integration.md) 에 실제로 치는 명령이 순서대로 있습니다.**
> 아래는 일정 관점의 큰 그림입니다.


```
1주차   ① 실데이터        ② 가짜DB로 점수엔진      ③ example.json으로 화면
           ↓                    ↓                        ↓
2주차   ①──────붙임──────②                          ③ 계속
                    ↓
3주차          ②──────붙임──────③   (fetch 경로만 교체)
```

- **① ↔ ② 를 먼저 붙입니다.** 이게 붙어야 제품이 성립합니다.
- ③은 마지막에 `fetch('contracts/score.example.json')` → `fetch('/api/opinion?ticker=...')` 한 줄 교체.
