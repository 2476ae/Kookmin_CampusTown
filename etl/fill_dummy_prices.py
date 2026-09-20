"""실DB에 가격·거시를 더미로 채웁니다. 시간이 없을 때 통합을 막지 않으려는 용도.

    python etl/fill_dummy_prices.py                    # data/stocks.db
    python etl/fill_dummy_prices.py data/stocks.db

재무는 담당 ① 의 진짜 데이터를 쓰고, 가격만 샘플로 채웁니다.
그러면 수익성·성장성·안정성 3축은 **진짜**이고 밸류에이션 축만 가짜입니다.

`meta.price_source = 'dummy'` 를 박아두면 엔진이 `flag.dummy_price` 경고를
붙이고 화면에 "가격 데이터가 샘플입니다" 가 뜹니다.
초보자에게 가짜 PER 을 진짜처럼 보여주면 안 됩니다.

진짜 가격이 생기면 이 스크립트를 다시 돌리지 말고 덮어쓰면 됩니다.
meta 의 price_source 만 바꾸면 경고도 사라집니다.

--- 왜 난수를 그냥 안 쓰나 -------------------------------------------------
시가총액을 순수 난수로 만들면 PER 분포가 엉망이 되어 백분위가 무의미해지고,
화면이 고장 난 것처럼 보입니다. 그래서 자본에 현실적인 PBR 분포를 곱해서
시가총액을 만듭니다 — PER 은 순이익에서 자연스럽게 따라옵니다.
"""
import pathlib
import random
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED = 20260920          # 고정. 같은 DB면 같은 가격이 나옵니다.
ASOF = "2026-09-20"


def fill(db):
    db = pathlib.Path(db)
    if not db.exists():
        print(f"DB 가 없습니다: {db}")
        return 1
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    rows = conn.execute(
        "SELECT c.ticker, "
        "  (SELECT value FROM annual_fact f WHERE f.cik=c.cik AND f.metric='equity' "
        "   ORDER BY fiscal_year DESC LIMIT 1) AS equity, "
        "  (SELECT value FROM annual_fact f WHERE f.cik=c.cik AND f.metric='net_income' "
        "   ORDER BY fiscal_year DESC LIMIT 1) AS net_income "
        "FROM company c WHERE c.delisted_date IS NULL").fetchall()

    rnd = random.Random(SEED)
    conn.execute("DELETE FROM price_snapshot WHERE asof = ?", (ASOF,))
    made = skipped = 0
    for ticker, equity, net_income in rows:
        if not equity or equity <= 0:
            skipped += 1        # 자본잠식·결측은 가격을 안 만듭니다 (엔진이 결측으로 처리)
            continue
        pbr = rnd.lognormvariate(1.0, 0.8)          # 중앙값 약 2.7배
        market_cap = equity * pbr
        close = rnd.uniform(8, 400)
        aligned = rnd.choice([None, None, 5, 35, 120, 260])
        conn.execute(
            "INSERT INTO price_snapshot "
            "(ticker, asof, close, market_cap, ma20, ma60, ma110, aligned_days) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ticker, ASOF, close, market_cap,
             close * rnd.uniform(.95, 1.05), close * rnd.uniform(.9, 1.1),
             close * rnd.uniform(.85, 1.15), aligned))
        made += 1

    # 거시는 여기서 안 만듭니다. 예전엔 숫자를 박아넣었는데 전부 틀렸고
    # (CPI 지수를 상승률로 착각), 그 값이 LLM 을 거쳐 사용자에게 나갔습니다.
    # FRED 는 키 없이 받아지니 etl/fetch_macro.py 를 쓰세요.

    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("price_source", "dummy"),
        ("price_note", "etl/fill_dummy_prices.py 가 만든 샘플입니다. PER·PBR 은 진짜가 아닙니다"),
        ("built_at", ASOF),
    ])
    conn.commit()
    conn.close()

    print(f"{db}")
    print(f"  가격 생성   {made:,}개")
    print(f"  건너뜀      {skipped:,}개 (자본 결측·자본잠식)")
    print(f"  거시        건드리지 않음 -> python etl/fetch_macro.py 로 채우세요")
    print(f"  meta        price_source = dummy  -> 화면에 경고 배지가 뜹니다")
    print(f"\n다음: python scoring/check_db.py {db}")
    return 0


if __name__ == "__main__":
    sys.exit(fill(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "stocks.db"))
