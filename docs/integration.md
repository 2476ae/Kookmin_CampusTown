# 통합 절차

시간이 없을 때 **10분 안에** 셋을 붙이는 절차입니다. 순서대로 치면 됩니다.

```
담당 ① DuckDB ──[1]──▶ data/stocks.db ──[2]──▶ 가격 채움 ──[3]──▶ 검사
                                                                    │
담당 ③ web/index.html ──────────────────────────────────────────[4]─┴──▶ python api/server.py
```

---

## 0. DB 를 어떻게 받나 — 두 가지 길

**담당 ① 의 3.2GB DuckDB 는 받을 필요가 없습니다.** 그건 분석용이고,
서빙에 필요한 건 거기서 뽑아낸 8MB 짜리 `stocks.db` 입니다.

### A. 직접 만든다 (담당 ① 에게 CSV 하나만 받으면 됨)

SEC 원본은 공개라 누구나 받을 수 있습니다. 분기 zip 2~4개(각 65~85MB)면 됩니다.

```bash
curl -O https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q1.zip
curl -O https://www.sec.gov/files/dera/data/financial-statement-data-sets/2025q4.zip
python etl/export_to_contract.py --from-zip 2026q1.zip 2025q4.zip --out data/stocks.db
python etl/fill_dummy_prices.py data/stocks.db
python scoring/check_db.py data/stocks.db
```

담당 ① 에게 받을 건 **상장폐지 CSV 하나뿐**입니다 (`cik,delisted_date`, 수십 KB).
그게 없으면 S6 배지가 안 뜹니다. `--delisted delisted.csv` 로 넣습니다.

### B. 담당 ① 이 만들어서 보낸다

`stocks.db` 파일 하나 (분기 2개 기준 **8MB**, gzip 2MB).
카톡·메일·디스코드로 그냥 보내집니다. 링크가 필요하면 GitHub Release 첨부.
**받으면 반드시 `python scoring/check_db.py data/stocks.db` 를 돌리세요** —
전송 중 깨졌는지, 버전이 맞는지 한 번에 나옵니다.

---

## 1. 담당 ① — DB 내보내기

```bash
python etl/export_to_contract.py --from-zip 2026q1.zip 2025q4.zip --out data/stocks.db
```

DuckDB 를 쓰고 있으면 `_read_zips()` 만 SELECT 로 바꾸면 됩니다.
실제 SEC 데이터로 검증했습니다:

| 넣은 분기 | 종목 | 재무 행 | 티커 매칭 |
|---|---|---|---|
| 2026q1 | 4,244 | 68,115 | 3,790 |
| 2026q1 + 2025q4 | **4,584** | **73,799** | **4,090** |

분기 하나로도 과거 비교수치 덕에 **2009~2026** 이 나옵니다. 분기를 더 넣으면
연도가 아니라 **회사 수**가 늘어납니다 (회계연도 말이 다른 회사들).

**조용히 틀리는 세 곳** (에러가 안 나서 위험합니다):

| | |
|---|---|
| `qtrs` | `0` = 시점 값(자산·부채·자본), `4` = 연간 기간 값(매출·이익·현금흐름) |
| `segments` / `coreg` | 비어 있는 행만. 아니면 사업부문별 값이 섞입니다 |
| `cik` 패딩 | SEC 는 `'6955'`, 계약은 `'0000006955'`. 안 맞추면 좌측 드릴다운이 원본을 못 찾습니다 |

`liabilities` 폴백(`assets - equity`)은 **필수**입니다 — 실측으로 4,244개 중 **1,130개(27%)** 가 필요했습니다.

### 이 파일에 뭐가 들어 있나

내보내기가 채우는 것 (SEC 2026q1 실측):

| | 채움 | 없으면 |
|---|---|---|
| `cik` · `name` · 재무 7종 | 100% | — |
| `sic` | 96% | 비교군을 못 만듭니다 |
| `ticker` | 89% (나머지는 CIK) | 화면에 `0000320193` 이 뜹니다 |
| `sic_desc` | 96% | 비교군 라벨이 `None` |
| `exchange` | 89% | NASDAQ/NYSE 구분 불가 |
| **`delisted_date`** | **0%** | **S6 상장폐지 배지가 통째로 죽습니다** |

`ticker` · `sic_desc` · `exchange` 는 SEC 공개 파일에서 자동으로 받습니다
(523KB + 109KB, `data/raw/` 에 캐시). SEC 가 User-Agent 에 연락처를 요구해서
`.env` 에 한 줄이 필요합니다:

```
SEC_CONTACT_EMAIL=you@example.com
```

**`delisted_date` 는 담당 ① 의 목록이 있어야 합니다.** Form 25-NSE 에서
1,103건을 이미 수집해뒀고 ETF·우선주·SPAC 도 걸러놨으니, `cik,delisted_date`
두 컬럼 CSV 로 내보내서 넘겨주면 됩니다:

```bash
python etl/export_to_contract.py --from-zip 2026q1.zip        --delisted delisted.csv --out data/stocks.db
```

## 2. 가격·거시가 아직 없으면 — 더미로 채웁니다

```bash
python etl/fill_dummy_prices.py data/stocks.db
```

재무는 진짜, 가격만 샘플입니다. 그러면 **수익성·성장성·안정성 3축은 진짜**이고
밸류에이션 축만 가짜입니다.

`meta.price_source = 'dummy'` 가 박히고, 엔진이 **`flag.dummy_price` 경고**를 붙입니다.
화면에 "가격 데이터가 샘플입니다 — PER·PBR 은 실제 값이 아닙니다" 가 뜹니다.
초보자에게 가짜 PER 을 진짜처럼 보여주면 안 되니 이건 빼지 마세요.

진짜 가격이 생기면 `price_snapshot` 을 덮어쓰고 `meta.price_source` 만 바꾸면
경고가 사라집니다.

## 3. 검사

```bash
python scoring/check_db.py data/stocks.db
```

**FAIL 이면 붙이지 마세요** (exit 1). 테이블·PK·metric 이름·SIC 커버리지·연도 범위를
보고, 실제로 엔진을 돌려 몇 종목에 점수가 나오는지까지 알려줍니다.

실측 참고치 (SEC 2026q1 + 더미 가격):

```
종목 4,244 · SIC 96% · 점수 나오는 종목 4,033 (95%)
비교군: SIC 4자리 1,462 · 3자리 595 · 2자리 1,157 · 전체 시장 819
```

## 4. 담당 ③ — 화면

`web/` 에 놓으면 **서버가 그대로 서빙합니다.** `index.html` 을 따로 열 필요 없습니다.

```bash
python api/server.py     # http://localhost:8000 에서 화면과 API가 같이 뜹니다
```

같은 주소라 **CORS 걱정 없이 상대경로**로 부르면 됩니다:

```js
fetch('/api/score?ticker=' + ticker)              // 즉시
new EventSource('/api/opinion?ticker=' + ticker)  // score -> status -> delta -> opinion -> done
```

목 JSON 으로 만들었다면 바꿀 건 **fetch 경로 한 줄**입니다:

```js
fetch('contracts/score.example.json')  ->  fetch('/api/score?ticker=' + ticker)
```

---

## 붙고 나서 확인할 것

```bash
python scoring/test_engine.py     # 19개
python llm/test_opinion.py        # 19개
```

검사는 **항상 가짜 DB·목 모드로 돕니다.** 실DB 가 있어도, API 키가 있어도
실제 API 를 치지 않습니다 (돈이 듭니다). 둘 다 `STOCKS_DB` 를 못 박아서
막아뒀고, 그게 안 걸리면 검사 자체가 실패합니다.

## 자주 막히는 곳

| 증상 | 원인 |
|---|---|
| 서버가 "DB 파일이 없습니다" | `data/stocks.db` 도 `data/fake.db` 도 없음 → `python scoring/fake_db.py` |
| "DB에 테이블이 없습니다" | ETL 도중인 파일. 내보내기가 안 끝났습니다 |
| 종목은 뜨는데 점수가 전부 `null` | 가격·거시가 비었습니다 → 2번 |
| 화면이 빈칸투성이 | `check_db.py` 의 "지표 계산 성공률" 확인. 재무 결측이 많습니다 |
| DB 를 갱신했는데 안 바뀜 | **서버 재시작이 필요합니다.** 시작할 때 한 번만 읽습니다 |
| 검사가 갑자기 깨짐 | 실DB 를 `data/fake.db` 로 덮어썼는지 확인 |

## 남은 것 (시간 되면)

- **가격 소스** — 프로토타입은 더미/yfinance. 공개 전환 시 유료 라이선스 필요
  ([근거](reply-price-source-and-score-axes.md))
- **티커 매핑** — 참고 구현은 `ticker` 에 CIK 를 넣습니다. 담당 ① 의
  `ticker_exchange` 를 붙이면 화면에 `AAPL` 이 뜹니다
- **미설계 화면 S2·S4·S7** — [screens.md](screens.md)
