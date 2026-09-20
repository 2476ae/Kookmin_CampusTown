"""FRED 에서 거시 지표를 받아 macro 테이블에 넣습니다. API 키 불필요.

    python etl/fetch_macro.py [data/stocks.db]

왜 따로 있나
------------
처음엔 fill_dummy_prices.py 가 숫자를 박아넣었는데 **틀렸습니다**:
  기준금리 4.25 (실제 3.63) · 실업률 4.4 (실제 4.1)
  CPI 3.1  <- CPIAUCSL 은 물가 '지수'(332.813)인데 상승률로 착각했습니다

그 값이 LLM 프롬프트로 들어가서 사용자에게 "물가상승률은 3.1%" 라고
나갔습니다. 근거를 대는 서비스에서 근거가 틀리면 제품이 성립하지 않습니다.

CPI 는 지수를 그대로 넘기면 안 됩니다 — "물가 지수 332.8" 은 초보자에게
아무 의미가 없습니다. 12개월 전 대비 상승률로 바꿔서 넣습니다.
"""
import csv
import io
import pathlib
import sqlite3
import sys
import requests   # urllib 는 FRED 의 WAF 에 막힙니다 (ConnectionResetError)

CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

# (시리즈, 우리 키, 설명, 변환)
#   level  : 값을 그대로 (%로 발표되는 것)
#   yoy    : 12개월 전 대비 상승률 (지수로 발표되는 것)
SERIES = [
    ("FEDFUNDS", "FEDFUNDS", "기준금리 (%)", "level"),
    ("UNRATE", "UNRATE", "실업률 (%)", "level"),
    ("CPIAUCSL", "CPI_YOY", "소비자물가 상승률 (전년동월비 %)", "yoy"),
]


def _series(series_id):
    r = requests.get(CSV.format(series_id), timeout=60)
    r.raise_for_status()
    text = r.text
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or not row[0][:4].isdigit():
            continue
        try:
            rows.append((row[0], float(row[1])))
        except ValueError:
            continue          # FRED 는 결측을 '.' 으로 표시합니다
    return rows


def fetch(db):
    db = pathlib.Path(db)
    if not db.exists():
        print(f"DB 가 없습니다: {db}")
        return 1
    conn = sqlite3.connect(db)
    out = []
    for series_id, key, label, how in SERIES:
        rows = _series(series_id)
        if not rows:
            print(f"  !! {series_id} 를 못 받았습니다")
            continue
        date, value = rows[-1]
        if how == "yoy":
            # 12개월 전과 비교. 월간 시리즈라 12칸 뒤.
            if len(rows) < 13:
                print(f"  !! {series_id} 이력이 짧아 전년동월비를 못 냅니다")
                continue
            value = (value / rows[-13][1] - 1) * 100
        out.append((key, date, round(value, 2), label))

    conn.execute("DELETE FROM macro")
    conn.executemany("INSERT INTO macro VALUES (?,?,?)",
                     [(k, d, v) for k, d, v, _ in out])
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('macro_source', 'fred')")
    conn.commit()
    conn.close()

    print(f"{db}")
    for k, d, v, label in out:
        print(f"  {k:10} {v:>8.2f}   {label}   ({d})")
    return 0


if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.exit(fetch(sys.argv[1] if len(sys.argv) > 1 else root / "data" / "stocks.db"))
