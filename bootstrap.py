"""클론 직후 한 번 실행하면 DB까지 준비됩니다. 남는 건 LLM API 키뿐입니다.

    python bootstrap.py                    # 알아서 찾습니다
    python bootstrap.py --duckdb sec.duckdb
    python bootstrap.py --zip 2026q1.zip 2025q4.zip

여러 번 돌려도 안전합니다. 이미 된 단계는 건너뜁니다.

하는 일
  1. 의존성 설치 (openai, requests, 그리고 DuckDB 를 쓰면 duckdb)
  2. .env 만들기 — 없으면 .env.example 에서 복사하고 한 줄씩 물어봅니다
  3. 원본 -> data/stocks.db 변환 (DuckDB 또는 SEC 분기 zip)
  4. 거시 지표 받기 (FRED, 키 불필요)
  5. 가격을 샘플로 채우기 (진짜 가격이 이미 있으면 건너뜀)
  6. 계약 검사
"""
import argparse
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "stocks.db"
STEP = 0


PLACEHOLDER = ("...", "example.com", "you@", "sk-...")


def _is_placeholder(v):
    """.env.example 의 자리표시자를 진짜 값으로 착각하지 않습니다.

    sk-... 때와 같은 덫입니다. you@example.com 은 '@' 가 있어서 이메일 검사를
    통과하고, 그 상태로 SEC 에 가짜 연락처를 보내게 됩니다.
    """
    low = (v or "").strip().lower()
    return not low or any(p in low for p in PLACEHOLDER)


def step(title):
    global STEP
    STEP += 1
    print(f"\n[{STEP}] {title}")


def run(args, why):
    """하위 스크립트를 돌립니다. 실패하면 거기서 멈추고 이유를 남깁니다."""
    r = subprocess.run([sys.executable, *args], cwd=ROOT)
    if r.returncode != 0:
        print(f"\n  !! 실패: {why}")
        print(f"     다시 돌려보려면: python {' '.join(args)}")
        raise SystemExit(1)


def ensure_deps(need_duckdb):
    step("의존성 확인")
    missing = []
    for mod, pkg in [("openai", "openai"), ("requests", "requests")] + \
                    ([("duckdb", "duckdb")] if need_duckdb else []):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        print("  이미 설치돼 있습니다")
        return
    print(f"  설치: {', '.join(missing)}")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", *missing])
    if r.returncode != 0:
        raise SystemExit(f"\n  !! pip 설치 실패. 직접 돌려보세요:\n"
                         f"     pip install -U {' '.join(missing)}")


def ensure_env(interactive=True):
    """.env 를 만들고 값을 채웁니다. 여기가 사람 손이 필요한 유일한 곳입니다."""
    step(".env 확인")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists():
        shutil.copy(example, env)
        print(f"  {env.name} 를 만들었습니다")

    # 파일만 읽고 끝내면 안 됩니다 — 아래 단계들이 os.environ 을 봅니다.
    sys.path.insert(0, str(ROOT / "llm"))
    from opinion import load_dotenv          # 프로젝트의 .env 로더
    load_dotenv(env)

    text = env.read_text(encoding="utf-8")
    need = {}
    for key, prompt, required in [
        ("SEC_CONTACT_EMAIL",
         "SEC 가 요구하는 연락처 이메일 (티커·업종명을 받는 데 씁니다)", False),
        ("OPENAI_API_KEY",
         "OpenAI API 키 (없으면 의견이 목 데이터로 나옵니다)", False),
    ]:
        cur = os.environ.get(key, "").strip()
        ok = not _is_placeholder(cur) and (("@" in cur) if "EMAIL" in key else True)
        if ok:
            print(f"  {key:20} 설정됨")
        elif interactive and sys.stdin.isatty():
            got = input(f"  {key} — {prompt}\n    입력 (건너뛰려면 엔터): ").strip()
            if got:
                need[key] = got
        else:
            print(f"  {key:20} 비어 있음 — {prompt}")

    if need:
        lines = [ln for ln in text.splitlines()
                 if not any(ln.strip().startswith(k + "=") for k in need)]
        lines += [f"{k}={v}" for k, v in need.items()]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for k in need:
            os.environ[k] = need[k]
        print(f"  {env.name} 에 {len(need)}개 저장했습니다")


def find_source(args):
    """원본을 찾습니다. 인자로 주면 그걸, 아니면 흔한 자리를 뒤집니다."""
    if args.duckdb:
        return "duckdb", [pathlib.Path(args.duckdb)]
    if args.zip:
        return "zip", [pathlib.Path(z) for z in args.zip]

    for d in (ROOT, DATA, DATA / "raw", ROOT.parent):
        ducks = sorted(d.glob("*.duckdb")) if d.is_dir() else []
        if ducks:
            return "duckdb", [ducks[0]]
    zips = sorted(set(sum([list(d.glob("20*q[1-4].zip"))
                           for d in (ROOT, DATA, DATA / "raw") if d.is_dir()], [])))
    if zips:
        return "zip", zips
    return None, []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duckdb", help="담당 ① 의 DuckDB 파일")
    ap.add_argument("--zip", nargs="+", help="SEC 분기 zip (여러 개 가능)")
    ap.add_argument("--sub-table", default="sub")
    ap.add_argument("--num-table", default="num")
    ap.add_argument("--delisted", help="상장폐지 목록 CSV (cik,delisted_date)")
    ap.add_argument("--real-prices", action="store_true",
                    help="원본에 진짜 가격이 있으므로 샘플로 덮지 않습니다")
    ap.add_argument("--yes", action="store_true", help="물어보지 않습니다")
    a = ap.parse_args()

    print("=" * 62, flush=True)
    print(" Kookmin CampusTown — 클론 직후 준비")
    print("=" * 62, flush=True)

    kind, sources = find_source(a)
    if kind is None:
        raise SystemExit(
            "\n원본을 못 찾았습니다.\n"
            "  DuckDB 가 있으면 : python bootstrap.py --duckdb <경로>\n"
            "  SEC 분기 zip 이면 : python bootstrap.py --zip 2026q1.zip 2025q4.zip\n"
            "  둘 다 없으면 SEC 에서 받으세요 (분기당 65~85MB):\n"
            "    https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q1.zip")
    print(f"\n원본: {kind} — {', '.join(p.name for p in sources)}")

    ensure_deps(need_duckdb=(kind == "duckdb"))
    ensure_env(interactive=not a.yes)

    step("원본 -> data/stocks.db 변환")
    if DB.exists():
        print(f"  기존 {DB.name} 를 {DB.name}.bak 로 보관합니다")
        shutil.copy(DB, DB.with_suffix(".db.bak"))
    cmd = ["etl/export_to_contract.py", "--out", str(DB)]
    cmd += (["--from-duckdb", str(sources[0]),
             "--sub-table", a.sub_table, "--num-table", a.num_table]
            if kind == "duckdb" else ["--from-zip", *map(str, sources)])
    if a.delisted:
        cmd += ["--delisted", a.delisted]
    if _is_placeholder(os.environ.get("SEC_CONTACT_EMAIL", "")):
        cmd += ["--no-fetch"]
        print("  (SEC_CONTACT_EMAIL 이 없어 티커·업종명은 건너뜁니다 — "
              "화면에 CIK 가 뜹니다)")
    run(cmd, "원본 변환")

    step("거시 지표 (FRED)")
    run(["etl/fetch_macro.py", str(DB)], "거시 지표 받기")

    step("가격")
    if a.real_prices:
        print("  --real-prices 라 건드리지 않습니다")
    else:
        run(["etl/fill_dummy_prices.py", str(DB)], "샘플 가격 채우기")

    step("계약 검사")
    r = subprocess.run([sys.executable, "scoring/check_db.py", str(DB)], cwd=ROOT)

    print("\n" + "=" * 62)
    if r.returncode != 0:
        print(" 검사에 FAIL 이 있습니다. 위 메시지를 보고 고친 뒤 다시 돌리세요.")
        raise SystemExit(1)

    has_key = not _is_placeholder(os.environ.get("OPENAI_API_KEY", ""))
    print(" 준비 끝.  python api/server.py  로 띄우세요.")
    if not has_key:
        print("\n 남은 것 하나 — .env 의 OPENAI_API_KEY")
        print("   없어도 화면·점수는 전부 돌아갑니다. 의견 글만 목 데이터로 나옵니다.")
    print("=" * 62)


if __name__ == "__main__":
    main()
