#!/usr/bin/env python3
"""Shared corporate-number lookup and sync helpers."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


DOWNLOAD_URL = "https://www.houjin-bangou.nta.go.jp/download/zenken/index.html"
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = WORKSPACE_ROOT / "data" / "cache" / "corporate_number"

COLUMNS = [
    "sequence_number",
    "corporate_number",
    "process",
    "correct",
    "update_date",
    "change_date",
    "legal_name",
    "legal_name_image_id",
    "kind",
    "prefecture_name",
    "city_name",
    "street_number",
    "address_image_id",
    "prefecture_code",
    "city_code",
    "post_code",
    "address_outside",
    "address_outside_image_id",
    "close_date",
    "close_cause",
    "successor_corporate_number",
    "change_cause",
    "assignment_date",
    "latest",
    "en_name",
    "en_prefecture_name",
    "en_city_name",
    "en_address_outside",
    "furigana",
    "hihyoji",
]


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", "", normalized).casefold()


def strip_company_suffix(value: str) -> str:
    normalized = normalize_text(value)
    for suffix in [
        "株式会社",
        "有限会社",
        "合同会社",
        "合名会社",
        "合資会社",
        "(株)",
        "㈱",
        "kabushikigaisha",
        "inc.",
        "inc",
        "corp.",
        "corp",
        "corporation",
        "co.,ltd.",
        "co.ltd.",
        "co.,ltd",
        "coltd",
    ]:
        normalized = normalized.replace(normalize_text(suffix), "")
    return normalized


def default_db_path(cache_dir: Path) -> Path:
    return cache_dir / "corporate_numbers.sqlite3"


def connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection, reset: bool = False) -> None:
    if reset:
        conn.executescript(
            """
            DROP TABLE IF EXISTS corporate_entities;
            DROP TABLE IF EXISTS imports;
            """
        )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS corporate_entities (
            corporate_number TEXT PRIMARY KEY,
            legal_name TEXT NOT NULL,
            legal_name_normalized TEXT NOT NULL,
            legal_name_core_normalized TEXT NOT NULL,
            furigana TEXT,
            furigana_normalized TEXT,
            kind TEXT,
            prefecture_name TEXT,
            city_name TEXT,
            street_number TEXT,
            post_code TEXT,
            close_date TEXT,
            close_cause TEXT,
            successor_corporate_number TEXT,
            assignment_date TEXT,
            update_date TEXT,
            latest TEXT,
            en_name TEXT,
            source_file TEXT
        );

        CREATE TABLE IF NOT EXISTS imports (
            source_file TEXT PRIMARY KEY,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            row_count INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_corporate_entities_legal_name
            ON corporate_entities(legal_name);
        CREATE INDEX IF NOT EXISTS idx_corporate_entities_legal_name_normalized
            ON corporate_entities(legal_name_normalized);
        CREATE INDEX IF NOT EXISTS idx_corporate_entities_furigana_normalized
            ON corporate_entities(furigana_normalized);
        CREATE INDEX IF NOT EXISTS idx_corporate_entities_pref_city
            ON corporate_entities(prefecture_name, city_name);
        """
    )
    conn.commit()


def row_to_record(row: list[str], source_file: str) -> dict[str, str]:
    padded = row + [""] * (len(COLUMNS) - len(row))
    data = dict(zip(COLUMNS, padded))
    legal_name = data["legal_name"]
    return {
        "corporate_number": data["corporate_number"],
        "legal_name": legal_name,
        "legal_name_normalized": normalize_text(legal_name),
        "legal_name_core_normalized": strip_company_suffix(legal_name),
        "furigana": data["furigana"],
        "furigana_normalized": normalize_text(data["furigana"]),
        "kind": data["kind"],
        "prefecture_name": data["prefecture_name"],
        "city_name": data["city_name"],
        "street_number": data["street_number"],
        "post_code": data["post_code"],
        "close_date": data["close_date"],
        "close_cause": data["close_cause"],
        "successor_corporate_number": data["successor_corporate_number"],
        "assignment_date": data["assignment_date"],
        "update_date": data["update_date"],
        "latest": data["latest"],
        "en_name": data["en_name"],
        "source_file": source_file,
    }


def import_csv_files(conn: sqlite3.Connection, csv_paths: list[Path]) -> None:
    init_db(conn)
    upsert_sql = """
        INSERT INTO corporate_entities (
            corporate_number, legal_name, legal_name_normalized,
            legal_name_core_normalized, furigana, furigana_normalized, kind,
            prefecture_name, city_name, street_number, post_code, close_date,
            close_cause, successor_corporate_number, assignment_date,
            update_date, latest, en_name, source_file
        )
        VALUES (
            :corporate_number, :legal_name, :legal_name_normalized,
            :legal_name_core_normalized, :furigana, :furigana_normalized, :kind,
            :prefecture_name, :city_name, :street_number, :post_code, :close_date,
            :close_cause, :successor_corporate_number, :assignment_date,
            :update_date, :latest, :en_name, :source_file
        )
        ON CONFLICT(corporate_number) DO UPDATE SET
            legal_name = excluded.legal_name,
            legal_name_normalized = excluded.legal_name_normalized,
            legal_name_core_normalized = excluded.legal_name_core_normalized,
            furigana = excluded.furigana,
            furigana_normalized = excluded.furigana_normalized,
            kind = excluded.kind,
            prefecture_name = excluded.prefecture_name,
            city_name = excluded.city_name,
            street_number = excluded.street_number,
            post_code = excluded.post_code,
            close_date = excluded.close_date,
            close_cause = excluded.close_cause,
            successor_corporate_number = excluded.successor_corporate_number,
            assignment_date = excluded.assignment_date,
            update_date = excluded.update_date,
            latest = excluded.latest,
            en_name = excluded.en_name,
            source_file = excluded.source_file
    """

    total = 0
    for csv_path in csv_paths:
        count = 0
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            batch = []
            for row in reader:
                batch.append(row_to_record(row, csv_path.name))
                if len(batch) >= 5000:
                    conn.executemany(upsert_sql, batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                conn.executemany(upsert_sql, batch)
                count += len(batch)
        conn.execute(
            "INSERT OR REPLACE INTO imports(source_file, row_count) VALUES (?, ?)",
            (csv_path.name, count),
        )
        conn.commit()
        total += count
        print(f"imported {count} rows from {csv_path}", file=sys.stderr)
    print(f"imported {total} rows total", file=sys.stderr)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "corporate-number-resolver/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def post_download(file_no: str, token: str) -> bytes:
    data = urllib.parse.urlencode(
        {
            "jp.go.nta.houjin_bangou.framework.web.common.CNSFWTokenProcessor.request.token": token,
            "event": "download",
            "selDlFileNo": file_no,
        }
    ).encode()
    request = urllib.request.Request(
        DOWNLOAD_URL,
        data=data,
        headers={
            "User-Agent": "corporate-number-resolver/0.1",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def html_to_text(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", value).split())


def parse_file_numbers(html: str, prefecture: str, section_id: str = "csv-unicode") -> list[tuple[str, str]]:
    section_start = html.find(f'id="{section_id}"')
    if section_start < 0:
        raise RuntimeError(f"download section not found: {section_id}")
    section_end = html.find("<h2", section_start + 10)
    section = html[section_start : section_end if section_end > 0 else len(html)]

    pref_index = section.find(prefecture)
    if pref_index < 0:
        raise RuntimeError(f"prefecture not found in download page: {prefecture}")
    pref_block_end = section.find("</dl>", pref_index)
    pref_block = section[pref_index : pref_block_end if pref_block_end > 0 else len(section)]

    matches = re.findall(r"doDownload\((\d+)\).*?>([^<]+)</a>", pref_block, flags=re.S)
    if not matches:
        raise RuntimeError(f"download file numbers not found for: {prefecture}")
    return [(file_no, html_to_text(label)) for file_no, label in matches]


def parse_token(html: str) -> str:
    match = re.search(
        r'name="jp\.go\.nta\.houjin_bangou\.framework\.web\.common\.CNSFWTokenProcessor\.request\.token" value="([^"]+)"',
        html,
    )
    if not match:
        raise RuntimeError("download token not found")
    return match.group(1)


def sync_prefecture(args: argparse.Namespace) -> None:
    cache_dir = Path(args.cache_dir).expanduser()
    target = "全国" if args.all else args.prefecture
    if not target:
        raise SystemExit("sync requires --all or --prefecture")

    data_dir = cache_dir / "downloads" / normalize_text(target)
    data_dir.mkdir(parents=True, exist_ok=True)

    html = fetch_text(DOWNLOAD_URL)
    token = parse_token(html)
    file_numbers = parse_file_numbers(html, target)
    print(f"found {len(file_numbers)} files for {target}", file=sys.stderr)

    csv_paths: list[Path] = []
    for file_no, label in file_numbers:
        zip_path = data_dir / f"{file_no}.zip"
        if args.force or not zip_path.exists():
            print(f"downloading {target} {label} ({file_no})", file=sys.stderr)
            zip_path.write_bytes(post_download(file_no, token))
        else:
            print(f"using cached {zip_path}", file=sys.stderr)

        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                if not member.endswith(".csv"):
                    continue
                output_path = data_dir / Path(member).name
                if args.force or not output_path.exists():
                    print(f"extracting {member}", file=sys.stderr)
                    archive.extract(member, data_dir)
                csv_paths.append(output_path)

    conn = connect(Path(args.db).expanduser())
    import_csv_files(conn, csv_paths)
    conn.close()


def format_address(row: sqlite3.Row) -> str:
    parts = [row["prefecture_name"], row["city_name"], row["street_number"]]
    return "".join(part or "" for part in parts)


def score_row(row: sqlite3.Row, query: str) -> float:
    query_norm = normalize_text(query)
    query_core = strip_company_suffix(query)
    name_norm = row["legal_name_normalized"] or ""
    name_core = row["legal_name_core_normalized"] or ""
    furigana = row["furigana_normalized"] or ""

    score = 0.0
    if query_norm == name_norm:
        score += 100
    elif query_core and query_core == name_core:
        score += 95
    elif name_norm.startswith(query_norm):
        score += 80
    elif query_norm in name_norm:
        score += 70
    elif query_core and query_core in name_core:
        score += 65
    elif query_norm in furigana:
        score += 45

    if not row["close_date"]:
        score += 10
    if row["latest"] == "1":
        score += 5
    if row["kind"] == "301":
        score += 2
    return score


def row_to_output(row: sqlite3.Row, query: str | None = None) -> dict[str, object]:
    output: dict[str, object] = {
        "corporate_number": row["corporate_number"],
        "legal_name": row["legal_name"],
        "furigana": row["furigana"],
        "kind": row["kind"],
        "address": format_address(row),
        "post_code": row["post_code"],
        "active": not bool(row["close_date"]),
        "close_date": row["close_date"] or None,
        "successor_corporate_number": row["successor_corporate_number"] or None,
        "assignment_date": row["assignment_date"],
        "update_date": row["update_date"],
        "source_file": row["source_file"],
    }
    if query is not None:
        output["match_score"] = score_row(row, query)
    return output


def search(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db).expanduser(), readonly=True)

    query_norm = normalize_text(args.query)
    query_core = strip_company_suffix(args.query)
    like_values = [f"%{query_norm}%"]
    clauses = ["legal_name_normalized LIKE ?", "furigana_normalized LIKE ?"]
    params: list[str] = [like_values[0], like_values[0]]
    if query_core and query_core != query_norm:
        clauses.append("legal_name_core_normalized LIKE ?")
        params.append(f"%{query_core}%")

    where = [f"({' OR '.join(clauses)})"]
    if args.prefecture:
        where.append("prefecture_name = ?")
        params.append(args.prefecture)
    if args.city:
        where.append("city_name = ?")
        params.append(args.city)
    if args.active_only:
        where.append("(close_date IS NULL OR close_date = '')")
    if args.company_only:
        where.append("kind = '301'")

    sql = f"""
        SELECT * FROM corporate_entities
        WHERE {' AND '.join(where)}
        LIMIT ?
    """
    params.append(str(max(args.limit * 10, args.limit)))
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    outputs = sorted((row_to_output(row, args.query) for row in rows), key=lambda item: item["match_score"], reverse=True)
    print(json.dumps(outputs[: args.limit], ensure_ascii=False, indent=2))


def lookup(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db).expanduser(), readonly=True)
    row = conn.execute(
        "SELECT * FROM corporate_entities WHERE corporate_number = ?",
        (args.corporate_number,),
    ).fetchone()
    conn.close()
    if row is None:
        print(json.dumps(None, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(row_to_output(row), ensure_ascii=False, indent=2))


def stats(args: argparse.Namespace) -> None:
    conn = connect(Path(args.db).expanduser(), readonly=True)
    row_count = conn.execute("SELECT COUNT(*) FROM corporate_entities").fetchone()[0]
    imports = conn.execute("SELECT source_file, row_count, imported_at FROM imports ORDER BY source_file").fetchall()
    conn.close()
    print(
        json.dumps(
            {
                "db": str(Path(args.db).expanduser()),
                "row_count": row_count,
                "imports": [dict(row) for row in imports],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def add_lookup_subcommands(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="search by legal-name fragment")
    search_parser.add_argument("query")
    search_parser.add_argument("--prefecture")
    search_parser.add_argument("--city")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--company-only", action="store_true", help="株式会社などの会社等(kind=301)だけに絞る")
    search_parser.add_argument("--include-closed", dest="active_only", action="store_false")
    search_parser.set_defaults(active_only=True, func=search)

    lookup_parser = subparsers.add_parser("lookup", help="lookup by corporate number")
    lookup_parser.add_argument("corporate_number")
    lookup_parser.set_defaults(func=lookup)

    stats_parser = subparsers.add_parser("stats", help="show database stats")
    stats_parser.set_defaults(func=stats)


def add_sync_subcommands(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="download CSV files and import them")
    sync_target = sync_parser.add_mutually_exclusive_group(required=True)
    sync_target.add_argument("--all", action="store_true", help="全国CSVを同期する。大容量。")
    sync_target.add_argument("--prefecture", help="例: 東京都。都道府県別CSVを同期する。")
    sync_parser.add_argument("--force", action="store_true")
    sync_parser.set_defaults(func=sync_prefecture)

    import_parser = subparsers.add_parser("import-csv", help="import existing NTA CSV files")
    import_parser.add_argument("csv_paths", nargs="+", type=Path)
    import_parser.add_argument("--reset", action="store_true")
    import_parser.set_defaults(
        func=lambda args: import_csv_files(connect(Path(args.db).expanduser()), args.csv_paths)
    )


def _build_base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help=f"cache and download directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument("--db", default=None)
    return parser


def build_lookup_parser() -> argparse.ArgumentParser:
    parser = _build_base_parser()
    add_lookup_subcommands(parser)
    return parser


def build_sync_parser() -> argparse.ArgumentParser:
    parser = _build_base_parser()
    add_sync_subcommands(parser)
    return parser


def build_full_parser() -> argparse.ArgumentParser:
    parser = _build_base_parser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    # lookup side
    search_parser = subparsers.add_parser("search", help="search by legal-name fragment")
    search_parser.add_argument("query")
    search_parser.add_argument("--prefecture")
    search_parser.add_argument("--city")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--company-only", action="store_true", help="株式会社などの会社等(kind=301)だけに絞る")
    search_parser.add_argument("--include-closed", dest="active_only", action="store_false")
    search_parser.set_defaults(active_only=True, func=search)

    lookup_parser = subparsers.add_parser("lookup", help="lookup by corporate number")
    lookup_parser.add_argument("corporate_number")
    lookup_parser.set_defaults(func=lookup)

    stats_parser = subparsers.add_parser("stats", help="show database stats")
    stats_parser.set_defaults(func=stats)

    # sync side
    sync_parser = subparsers.add_parser("sync", help="download CSV files and import them")
    sync_target = sync_parser.add_mutually_exclusive_group(required=True)
    sync_target.add_argument("--all", action="store_true", help="全国CSVを同期する。大容量。")
    sync_target.add_argument("--prefecture", help="例: 東京都。都道府県別CSVを同期する。")
    sync_parser.add_argument("--force", action="store_true")
    sync_parser.set_defaults(func=sync_prefecture)

    import_parser = subparsers.add_parser("import-csv", help="import existing NTA CSV files")
    import_parser.add_argument("csv_paths", nargs="+", type=Path)
    import_parser.add_argument("--reset", action="store_true")
    import_parser.set_defaults(
        func=lambda args: import_csv_files(connect(Path(args.db).expanduser()), args.csv_paths)
    )
    return parser


def _resolve_db(args: argparse.Namespace) -> None:
    if args.db is None:
        args.db = str(default_db_path(Path(args.cache_dir).expanduser()))


def _main_with_parser(parser: argparse.ArgumentParser, argv: list[str] | None = None) -> int:
    args = parser.parse_args(argv)
    _resolve_db(args)
    if getattr(args, "command", None) == "import-csv":
        conn = connect(Path(args.db).expanduser())
        init_db(conn, reset=args.reset)
        import_csv_files(conn, args.csv_paths)
        conn.close()
        return 0
    args.func(args)
    return 0


def run_lookup(argv: list[str] | None = None) -> int:
    return _main_with_parser(build_lookup_parser(), argv)


def run_sync(argv: list[str] | None = None) -> int:
    return _main_with_parser(build_sync_parser(), argv)


def run_full(argv: list[str] | None = None) -> int:
    return _main_with_parser(build_full_parser(), argv)
