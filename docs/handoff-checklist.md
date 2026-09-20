# 담당 ① → 팀: 보내주실 것

2026-09-20. 담당 ② 요청입니다. **파일 2개면 됩니다.**

> 3.2GB DuckDB 는 보내지 마세요. 원본 zip 도요. 분석용이라 서빙에는 안 씁니다.

---

## 1️⃣ `stocks.db` — 계약 형식 SQLite (약 8MB)

```bash
python etl/export_to_contract.py --from-zip 2026q1.zip 2025q4.zip \
       --delisted delisted.csv --out data/stocks.db
```

DuckDB 에서 바로 뽑으시려면 `etl/export_to_contract.py` 의 **`_read_zips()` 하나만**
SELECT 로 바꾸면 됩니다. 나머지 로직(태그 사다리·폴백·정규화)은 그대로 씁니다.

```python
con.execute("SELECT adsh, cik, name, sic, form, period, filed FROM sub")
con.execute("SELECT adsh, tag, ddate, qtrs, uom, segments, coreg, value FROM num")
```

### 스크립트를 안 쓰고 직접 만드실 거면

`contracts/db_schema.sql` 그대로, 테이블 4개(+선택 `meta`). 채워야 할 것:

| 테이블 | 필수 | 비고 |
|---|---|---|
| `company` | `cik` `ticker` `name` `sic` | `sic_desc` `exchange` 있으면 화면이 좋아집니다 |
| `annual_fact` | `cik` `fiscal_year` `metric` `value` | **metric 은 아래 7개 이름만** |
| `price_snapshot` | — | 비워두셔도 됩니다. 저희가 더미로 채웁니다 |
| `macro` | — | 비워두세요. `etl/fetch_macro.py` 가 FRED 에서 받습니다 |

`metric` 7개 — 이 철자 그대로여야 합니다:

```
revenue  operating_income  net_income  assets  liabilities  equity  operating_cashflow
```

### ⚠️ 조용히 틀리는 세 곳

에러가 안 나서 붙여봐야 알게 되는 것들입니다. **보내기 전에 확인 부탁드립니다.**

| | 확인 |
|---|---|
| **`cik` 패딩** | `'0000320193'` 10자리. SEC 원본은 `'320193'` 입니다. 안 맞추면 좌측 드릴다운이 원본을 못 찾습니다 |
| **`qtrs`** | `0` = 시점 값(자산·부채·자본) / `4` = 연간 기간 값(매출·이익·현금흐름). 섞이면 분기 숫자가 연간으로 들어갑니다 |
| **`segments` `coreg`** | 비어 있는 행만. 아니면 사업부문별 값이 섞입니다 (이미 알고 계신 부분) |

그리고 **`liabilities` 폴백(`assets - equity`)은 필수**입니다. 저희 실측으로
4,244개 중 **1,130개(27%)** 가 부채 태그 없이 폴백이 필요했습니다. 예외가 아니라 기본입니다.

---

## 2️⃣ `delisted.csv` — 상장폐지 목록 (수십 KB)

두 컬럼이면 됩니다.

```csv
cik,delisted_date
0000886158,2023-05-03
0000718877,2023-10-13
```

- `cik` 는 0패딩 있어도 없어도 받습니다 (저희 쪽에서 맞춥니다)
- `delisted_date` 는 Form 25 접수일. `YYYY-MM-DD`
- 이미 수집하신 **1,103건**(SPAC 제외) 그대로면 됩니다. 라벨(`bankruptcy` 등)은
  지금 안 씁니다 — 나중에 배지 내용물로 쓰자는 제안이 합의되면 그때 같이 받겠습니다

**이게 없으면 상장폐지 경고 배지가 한 번도 안 뜹니다.** 저희가 설계한 기능인데
지금 `delisted_date` 가 0% 라 통째로 죽어 있습니다.

---

## 3️⃣ 보내기 전에 한 번만

```bash
python scoring/check_db.py data/stocks.db
```

**FAIL 이 하나라도 있으면 저희가 못 씁니다.** 결과를 그대로 붙여서 같이 보내주시면,
받기 전에 뭐가 문제인지 알 수 있습니다.

통과 시 이런 모양입니다 (저희가 SEC 2026q1+2025q4 로 돌린 참고치):

```
  ok   종목 4,584개
  ok   SIC 있는 종목 4,417개 (96%)
  ok   재무 행 73,799개
  ok   annual_fact PRIMARY KEY ['cik', 'fiscal_year', 'metric']
  ok   연도 범위 2009~2026 (18년)
  ok   점수가 나오는 종목 4,365/4,584 (95%)
```

---

## 어떻게 보내나

`stocks.db` 8MB + `delisted.csv` 수십 KB 라 **메일·카톡·디스코드로 그냥 갑니다.**
gzip 하면 2MB 입니다. 링크가 편하시면 GitHub Release 첨부가 깔끔합니다
(2GB 까지, git 히스토리를 안 더럽힙니다).

**저장소에 커밋하지는 마세요.** `data/` 가 `.gitignore` 라 `git add` 가 거부하고,
`-f` 로 밀어넣으면 히스토리에 영구히 남습니다.

---

## 안 보내셔도 되는 것

| | 왜 |
|---|---|
| DuckDB 3.2GB | 분석용입니다. 서빙에는 안 씁니다 |
| 분기 zip 원본 | SEC 공개라 필요하면 저희가 받습니다 |
| Altman·Piotroski·Sloan 계산 결과 | 아직 계약에 없습니다. [점수 축 합의](reply-price-source-and-score-axes.md) 후에 |
| 가격 데이터 | 지금은 더미로 채웁니다 ([라이선스 사안](reply-price-source-and-score-axes.md)) |

---

## 받으면 저희가 하는 것

```bash
python scoring/check_db.py data/stocks.db   # 다시 한 번
python etl/fill_dummy_prices.py data/stocks.db
python api/server.py
```

자세한 건 [integration.md](integration.md).
