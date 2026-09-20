"""이미 만들어진 DB 에 상장폐지 목록만 덧씌웁니다.

    python etl/apply_delisted.py delisted.csv [data/stocks.db]

담당 ① 이 stocks.db 와 delisted.csv 를 따로 보내는 경우가 있습니다.
export_to_contract.py 의 --delisted 는 zip 에서 처음부터 만들 때만 쓰이므로,
받은 DB 에 나중에 붙이려면 이게 필요합니다.

delisted_date 가 없으면 S6 상장폐지 경고 배지가 한 번도 안 뜹니다.

CSV 형식 (헤더 필수):
    cik,delisted_date
    0000886158,2023-05-03
cik 은 0패딩이 있든 없든 받습니다. 날짜는 YYYY-MM-DD.
"""
import csv
import pathlib
import sqlite3
import sys


def apply(csv_path, db_path):
    csv_path, db_path = pathlib.Path(csv_path), pathlib.Path(db_path)
    for p in (csv_path, db_path):
        if not p.exists():
            print(f"없습니다: {p}")
            return 1

    rows = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
        cik_col = cols.get("cik")
        date_col = cols.get("delisted_date") or cols.get("form25_date") or cols.get("date")
        if not cik_col or not date_col:
            print(f"컬럼을 못 찾았습니다. 필요한 것: cik, delisted_date")
            print(f"  파일에 있는 것: {reader.fieldnames}")
            return 1
        for r in reader:
            cik = (r[cik_col] or "").strip()
            date = (r[date_col] or "").strip()
            if cik and date:
                rows[cik.zfill(10)] = date

    conn = sqlite3.connect(db_path)
    have = {r[0] for r in conn.execute("SELECT cik FROM company")}
    hit = {c: d for c, d in rows.items() if c in have}
    conn.executemany("UPDATE company SET delisted_date = ? WHERE cik = ?",
                     [(d, c) for c, d in hit.items()])
    conn.commit()
    n = conn.execute(
        "SELECT count(*) FROM company WHERE delisted_date IS NOT NULL").fetchone()[0]
    conn.close()

    print(f"{db_path}")
    print(f"  CSV 행        {len(rows):,}")
    print(f"  DB 에 매칭    {len(hit):,}")
    print(f"  못 찾은 cik   {len(rows) - len(hit):,}  (그 회사의 재무가 DB 에 없는 경우)")
    print(f"  최종 상폐표시 {n:,}개")
    if not hit:
        print("\n  !! 하나도 안 붙었습니다. cik 형식을 확인하세요 "
              "(DB 는 10자리 0패딩: '0000320193')")
    print(f"\n다음: python scoring/check_db.py {db_path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.exit(apply(sys.argv[1],
                   sys.argv[2] if len(sys.argv) > 2 else root / "data" / "stocks.db"))
