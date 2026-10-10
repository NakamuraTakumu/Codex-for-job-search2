#!/usr/bin/env python3
"""調査結果を1つの表にまとめた自己完結型 HTML を生成する。列の説明は schemas/ の description から取る（pydantic が必要）。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import calendar
import subprocess
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = WORKSPACE_ROOT / "data"
DEFAULT_OUTPUT = WORKSPACE_ROOT / "site" / "index.html"  # 公開用（暗号化）
DEFAULT_PLAIN_OUTPUT = WORKSPACE_ROOT / "tmp" / "viewer" / "index.html"  # ローカル確認用（平文。git 管理外であること）
MISSING = "—"
PASSWORD_ENV = "REPORT_ACCESS_PASSWORD"
ITERATIONS = 200_000  # 既存の暗号化ビューアと互換（PBKDF2-HMAC-SHA256 → AES-256-GCM）

sys.path.insert(0, str(WORKSPACE_ROOT))
from schemas.company_profile_models import BasicInformation, CompanyProfile, IndustryClassificationCode  # noqa: E402
from schemas.recruitment_case_models import ApplicationSlot, RecruitmentCase, ApplicationWindow, SelectionProcess, StartingSalary  # noqa: E402

SELECTION_FLAGS = [
    ("has_entry_sheet_submission", "ES"), ("has_web_test", "Web"),
    ("has_written_exam", "筆記"), ("has_written_assignment", "課題"),
    ("has_group_discussion", "GD"), ("has_group_interview", "集団面接"),
    ("has_interview", "面接"), ("has_case_interview", "ケース"),
    ("has_research_presentation", "研究発表"), ("has_coding_task", "コーディング"),
]
SELECTION_LABELS = [label for _f, label in SELECTION_FLAGS]
# 畳んだ「選考」列に出す、多くの枠にはない選考（表示順・1文字の記号）。ES・Web・筆記・面接は大半の枠にあるので出さない。
RARE_SELECTION = [("GD", "G"), ("集団面接", "集"), ("ケース", "ケ"), ("課題", "課"), ("研究発表", "研"), ("コーディング", "コ")]


def _num(x, scale: float, digits: int) -> str:
    s = f"{x / scale:,.{digits}f}"
    if digits and "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_money(v) -> str:
    """円の金額を億円単位の数値だけにする（例: 22,567.7, 2,903, 0.1）。0.05億円未満は「<0.1」。"""
    return "<0.1" if 0 < v < 5e6 else _num(v, 1e8, 1)


def fmt_date(v) -> str:
    """2027-03-31 -> 27/3/31。形式が違えばそのまま返す。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    return f"{m[1][2:]}/{int(m[2])}/{int(m[3])}" if m else str(v)


def fmt_date_range(v) -> str:
    """DateRange(dict) を表示用に。日 -> 26/11/15、上旬/中旬/下旬 -> 26/11上/中/下、月全体 -> 26/11、他は 26/11/5–11/20。"""
    try:
        e, l = date.fromisoformat(v["earliest"]), date.fromisoformat(v["latest"])
    except (TypeError, KeyError, ValueError):
        return str(v)
    ym = f"{e.year % 100}/{e.month}"
    if e == l:
        return f"{ym}/{e.day}"
    if (e.year, e.month) == (l.year, l.month):
        last = calendar.monthrange(e.year, e.month)[1]
        if (e.day, l.day) == (1, last):
            return ym
        part = {(1, 10): "上", (11, 20): "中", (21, last): "下"}.get((e.day, l.day))
        if part:
            return ym + part
    right = f"{l.month}/{l.day}" if l.year == e.year else f"{l.year % 100}/{l.month}/{l.day}"
    return f"{ym}/{e.day}–{right}"


def range_sort_key(v) -> str:
    """DateRange(dict) の並べ替えキー（earliest の ISO 文字列）。"""
    return str(v.get("earliest")) if isinstance(v, dict) else str(v)


def shift_range(v):
    """DateRange(dict) の earliest・latest を各々1年後ろへ（2/29 は翌年に無いので 2/28）。解釈できなければそのまま返す。"""
    try:
        out = {}
        for k in ("earliest", "latest"):
            d = date.fromisoformat(v[k])
            out[k] = (d.replace(year=d.year + 1) if not (d.month, d.day) == (2, 29) else date(d.year + 1, 2, 28)).isoformat()
    except (TypeError, KeyError, ValueError):
        return v
    return {**v, **out}


def shifted(v):
    """前年度の日付を1年後ろにずらした版: [表示文字列, ソートキー]。ずらさない版は fmt_date_range / range_sort_key。"""
    s = shift_range(v)
    return [fmt_date_range(s), range_sort_key(s)]


LEGAL_FORMS = "株式会社|有限会社|合同会社|合資会社|合名会社|[(（]株[)）]|[(（]有[)）]|[(（]合[)）]"
_LEGAL_RE = re.compile(rf"^(?:{LEGAL_FORMS})\s*|\s*(?:{LEGAL_FORMS})$")


def short_name(name: str) -> str:
    """法人格の表記（前置・後置）を除く。除いて空になる場合は元の名前のまま。"""
    return _LEGAL_RE.sub("", name).strip() or name


# 表示用の短い名前。スキーマの enum 値をキーにし、未知の値は生の値で表示する。
REMOTE_LABELS = {"full": "フル", "hybrid": "併用", "limited": "一部", "none": "なし"}
ERUBOSHI_LABELS = {"stage_1": "1段階", "stage_2": "2段階", "stage_3": "3段階", "platinum": "プラチナ"}
# 順序のある列挙値の並び（小さい方から）。並べ替えはこの順位で行い、表示ラベルとは独立。
ORDINAL = {
    "remote_work_policy": ["none", "limited", "hybrid", "full"],
    "eruboshi_certification_tier": ["stage_1", "stage_2", "stage_3", "platinum"],
}


def first_code_key(v):
    """業種コードの一覧は先頭のコードで並べ替える。"""
    return v[0] if isinstance(v, list) and v else v


def ordinal_key(field):
    order = ORDINAL[field]
    return lambda v: order.index(v) if v in order else len(order)


JSIC_NAMES = {  # 日本標準産業分類の大分類（先頭の英字）
    "A": "農林業", "B": "漁業", "C": "鉱業", "D": "建設", "E": "製造", "F": "電気・ガス",
    "G": "情報通信", "H": "運輸・郵便", "I": "卸売・小売", "J": "金融・保険", "K": "不動産",
    "L": "専門サービス", "M": "宿泊・飲食", "N": "生活関連", "O": "教育", "P": "医療・福祉",
    "Q": "複合サービス", "R": "サービス", "S": "公務", "T": "分類不能",
}
INDUSTRY_LABELS = {m.value: JSIC_NAMES[m.name[0]] for m in IndustryClassificationCode}


def enum_fmt(labels, sep=", "):
    return lambda v: sep.join(labels.get(x, str(x)) for x in (v if isinstance(v, list) else [v]))


# 初任給の表示規約（この対応の唯一の説明）。学位ごとに（学/修/博, 基本給, 固定残業代, その他）の4列。
# - 学/修/博の列は monthly_total_yen。null なら base_salary_yen で代用し、セルに「基」の接頭辞と
#   「月給額の記載がなく基本給のみ」の title を付ける（並べ替えは数値のまま）。
# - 内訳列は 基本給・固定残業代・その他。その他 = monthly_total − base − (fixed or 0) で、total と base が両方分かるときだけ表示。
# - 〃：学位別の列で、左の（値のある直近の）学位と表示・印・接頭辞・title が同じなら 〃 にする。基本給のみと月給額は同額でも別物。
# - 折りたたみ列（初任給）は学位別の月給額セルの最大値。同額なら低い学位で、接頭辞は学位の頭文字。
DEGREES = ["学士", "修士", "博士"]
SALARY_KEYS = {"学士": "starting_salary", "修士": "starting_salary_master", "博士": "starting_salary_doctor"}
SALARY_PARTS = ("基本給", "固定残業代", "その他")

# (field, label, formatter)。単位は列見出し（UNITS）に置き、セルは数値だけにする。
PROFILE_COLUMNS = [
    ("industry_classification_codes", "業種", enum_fmt(INDUSTRY_LABELS, "/")),
    ("average_annual_income_yen", "平均年収", lambda v: _num(v, 1e4, 0)),
    ("average_overtime_hours_per_month", "月残業", lambda v: _num(v, 1, 1)),
    ("annual_holidays_days", "年間休日", lambda v: _num(v, 1, 0)),
    ("paid_leave_usage_days", "有給取得", lambda v: _num(v, 1, 1)),
    ("remote_work_policy", "リモート制度", enum_fmt(REMOTE_LABELS)),
    ("new_graduate_turnover_rate_within_3_years_percent", "3年離職率", lambda v: _num(v, 1, 1)),
    ("average_tenure_years", "平均勤続", lambda v: _num(v, 1, 1)),
    ("average_age_years", "平均年齢", lambda v: _num(v, 1, 1)),
    ("eruboshi_certification_tier", "えるぼし", enum_fmt(ERUBOSHI_LABELS)),
    ("employee_count", "従業員数", lambda v: _num(v, 1, 0)),
    ("capital_yen", "資本金", fmt_money),
    ("research_and_development_expenses_yen", "研究開発費", fmt_money),
    ("established_year", "設立", lambda v: _num(v, 1, 0).replace(",", "")),
]
# 並べ替えの「良い順」の向き。+1 は大きいほど良い、-1 は小さいほど良い。ない列は昇順・降順のまま。
GOOD_DIRECTION = {
    **{c: 1 for c in ("初任給", "平均年収", "年間休日", "有給取得", "リモート制度", "平均勤続", "えるぼし", "研究開発費")},
    **{f"{d}{n}": 1 for d in DEGREES for n in ("初任給", "基本給", "その他")},
    **{c: -1 for c in ("月残業", "3年離職率")},
    **{f"{d}固定残業代": -1 for d in DEGREES},
}
UNITS = {
    "平均年収": "万円", "3年離職率": "%", "平均勤続": "年", "平均年齢": "歳", "月残業": "h",
    "年間休日": "日", "有給取得": "日", "設立": "年", "従業員数": "人", "資本金": "億円", "研究開発費": "億円",
    "初任給": "万円", **{f"{d}{n}": "万円" for d in DEGREES for n in ("初任給", "基本給", "固定残業代", "その他")},
}

# 表の見出しに出す短い名前。正式名（COLUMNS）と単位は見出しのポップアップ・ツールチップに出す。
SHORT = {
    "最初の募集開始": "初開", "最初の締切": "初締",
    **{f"{d}{n}": d[0] + s for d in DEGREES for n, s in (("初任給", "初"), ("基本給", "基"), ("固定残業代", "残"), ("その他", "他"))},
    "平均年収": "年収", "3年離職率": "離職", "平均勤続": "勤続", "平均年齢": "年齢",
    "月残業": "残業", "年間休日": "休日", "有給取得": "有給", "リモート制度": "リモ制", "従業員数": "社員",
    "資本金": "資本", "研究開発費": "研開",
    "業種1位": "1位", "業種2位": "2位", "業種3位": "3位",
    "集団面接": "集団", "研究発表": "研究", "コーディング": "コード",
}

# 業種は畳むと主な事業の1列、開くと1位〜3位の3列（取り出し元は同じ記録）。
INDUSTRY_POS = ["業種1位", "業種2位", "業種3位"]
COLUMNS = (
    ["配属先", "応募枠", "最初の募集開始", "最初の締切", "選考", *SELECTION_LABELS,
     "初任給"]
    + [f"{d}{n}" for d in DEGREES for n in ("初任給", *SALARY_PARTS)]
    + [x for c in PROFILE_COLUMNS for x in (c[1], *(INDUSTRY_POS if c[1] == "業種" else ()))]
    + ["採用元"]  # 行の企業情報ではないので、企業情報の後ろに独立して置く
)
COL_INDEX = {c: i for i, c in enumerate(COLUMNS)}
NUMERIC_COLUMNS = {"初任給"} | {f"{d}{n}" for d in DEGREES for n in ("初任給", *SALARY_PARTS)} | {
    c[1] for c in PROFILE_COLUMNS
    if c[0] not in ("remote_work_policy", "eruboshi_certification_tier",
                    "industry_classification_codes")
}


def fmt_bool(v) -> str:
    return "○" if v else "×"


def cell(text="—", sort=None, marks="", tip="", pre="", shift=None) -> list:
    """[表示文字列, ソートキー(数値/文字列/None), 印, (省略せず見せたい全文), (値の前に小さく出す文字), (前年度の日付を+1年した [表示文字列, ソートキー])]"""
    if shift:
        return [text, sort, marks, tip, pre, shift]
    return [text, sort, marks] + ([tip, pre] if pre else [tip] if tip else [])


MISSING_CELL = cell(MISSING)


def records(metric) -> list[dict]:
    return (metric or {}).get("records") or []


def kind_of(rec) -> str:
    return ((rec or {}).get("source") or {}).get("kind", "unofficial")


KIND_ORDER = ("official", "unofficial", "unknown")  # 候補の優先順（唯一の定義）


def _ordered(tiers):
    """tiers: [(records, tag)] を優先順（公式→非公式→不明、各々で現年→前年）に並べ、値を持つ記録だけ残す。"""
    return [([r for r in recs if kind_of(r) == kind and r.get("value") is not None], tag, kind != "official")
            for kind in KIND_ORDER for recs, tag in tiers]


def top(tiers):
    """tiers: [(records, is_prev)] を優先順に。最初に値を持つ階層の先頭の記録と印を返す（なければ None）。

    各階層は公式→非公式の順に分けて評価されるため、呼び出し側は
    (current, previous) の2要素を渡す。
    """
    for recs, prev, unofficial in _ordered(tiers):
        if not recs:
            continue
        marks = ("前" if prev else "") + ("非" if unofficial else "")
        if any(r["value"] != recs[0]["value"] for r in recs[1:]):
            marks += "+"
        return recs[0], marks
    return None


def pick(tiers, fmt, sortfn=None) -> list:
    t = top(tiers)
    if not t:
        return cell(MISSING)
    first = t[0]["value"]
    return cell(fmt(first), sortfn(first) if sortfn else first, t[1])


def num_fmt(f):
    """数値でない値（enum 値や想定外の型）は f に渡して、型が合わなければ生の値で表示する。"""
    def g(v):
        if isinstance(v, bool):
            return str(v)
        try:
            return f(v)
        except TypeError:
            return str(v)
    return g


def tiers_for(slots, getter):
    """slots: [(slot, is_prev)] 現年→前年。getter(slot) -> records"""
    return [(getter(s), prev) for s, prev in slots]


def window_date_cell(slots, field) -> list:
    """全応募回の field（応募開始日・締切）のうち最も早い日。"""
    for kind in KIND_ORDER:
        for slot, prev in slots:
            vals = []
            for w in (slot.get("application_windows") or {}).get("records") or []:
                if kind_of(w) != kind:
                    continue
                vals += [r["value"] for r in records(w.get(field)) if r.get("value")]
            if vals:
                v = min(vals, key=range_sort_key)
                return cell(fmt_date_range(v), range_sort_key(v), ("前" if prev else "") + ("非" if kind != "official" else ""),
                            shift=shifted(v) if prev else None)
    return cell(MISSING)


def selection_cells(slots) -> list:
    """[畳んだ「選考」セル, *SELECTION_FLAGS 順の各選考セル]。どちらも同じ pick の結果から作る。"""
    flags = {}
    for field, label in SELECTION_FLAGS:
        sp = lambda s, f=field: records(((s.get("selection_process") or {}).get(f)))
        flags[label] = pick(tiers_for(slots, sp), fmt_bool, lambda v: 2 if v else 1)  # あり > なし。不明は並べ替えで常に最後
    shown, marks = [], set()
    for label, glyph in RARE_SELECTION:
        if flags[label][0] == "○":
            shown.append((label, glyph))
            marks |= set(flags[label][2])
    folded = cell(" ".join(g for _l, g in shown), len(shown), "".join(m for m in "前非+" if m in marks),
                  "・".join(l for l, _g in shown))
    return [folded, *flags.values()]


# --- セルの詳細（値の根拠） ---------------------------------------------------
# 候補: [表示値, 年度(0=なし/1=今年度/2=前年度), 区分(公/非/?), URL, 採用(1/0), ラベル]
# 埋め込み時に URL は D.urls、note 本文は D.notes への添字へ置き換える（同文が多いため）。
# セル詳細: [候補のリスト, [[ラベル, note], ...]]。どちらも空なら None。
YEAR_LABELS = {0: "企業情報", 1: "今年度", 2: "前年度"}


def ycode(slot, year) -> int:
    return 1 if slot.get("application_year") == year else 2


def cand(text, y, rec, chosen=0, label="", shift=None) -> list:
    kind = {"official": "公", "unofficial": "非"}.get(((rec or {}).get("source") or {}).get("kind"), "?")
    return [text, y, kind, ((rec or {}).get("source") or {}).get("url") or "", chosen, label] + ([shift[0]] if shift else [])


def pack(cands, notes):
    return [cands, notes] if cands or notes else None


def metric_cands(tiers, fmt, label="") -> list:
    """優先順に全候補を並べ、採用された先頭の1件に印を付ける。"""
    out = [cand(fmt(r["value"]), y, r, 0, label) for recs, y, _u in _ordered(tiers) for r in recs]
    if out:
        out[0][4] = 1
    return out


def metric_notes(pairs, label="") -> list:
    """pairs: [(年度コード, metric)]。note のあるものだけ [ラベル, note] にする。"""
    return [[(label + " " if label else "") + YEAR_LABELS[y], n]
            for y, m in pairs if (n := ((m or {}).get("note") or "").strip())]


def metric_detail(slots, mget, fmt, year):
    metrics = [(ycode(s, year), mget(s)) for s, _ in slots]
    return pack(metric_cands([(records(m), y) for y, m in metrics], fmt), metric_notes(metrics))


def window_date_detail(slots, field, year):
    """全応募回の field を候補にし、window_date_cell が採用した値の先頭に印を付ける。"""
    chosen = window_date_cell(slots, field)[1]
    cands, notes, done = [], [], False
    for kind in KIND_ORDER:
        for slot, _prev in slots:
            y = ycode(slot, year)
            for w in (slot.get("application_windows") or {}).get("records") or []:
                if kind_of(w) != kind:
                    continue
                name = w.get("window_name") or MISSING
                for r in records(w.get(field)):
                    if r.get("value"):
                        hit = int(not done and range_sort_key(r["value"]) == chosen)
                        done = done or bool(hit)
                        cands.append(cand(fmt_date_range(r["value"]), y, {**r, "source": r.get("source") or w.get("source")}, hit, name,
                                     shifted(r["value"]) if y == 2 else None))
                notes += metric_notes([(y, w.get(field))], name)
    for slot, _prev in slots:
        notes += metric_notes([(ycode(slot, year), slot.get("application_windows"))], "募集回")
    return pack(cands, notes)


def selection_detail(slots, year):
    cands, notes = [], []
    for field, label in SELECTION_FLAGS:
        ms = [(ycode(s, year), (s.get("selection_process") or {}).get(field)) for s, _ in slots]
        cands += metric_cands([(records(m), y) for y, m in ms], fmt_bool, label)
        notes += metric_notes(ms, label)
    return pack(cands, notes)


def salary_metric(slot, key) -> dict:
    """学位列の値。月給額がなく基本給だけの記録は基本給で代用する（どちらかは必ずある。表示規約は SALARY_KEYS の注釈）。"""
    m = slot.get(key) or {}
    val = lambda v: v.get("monthly_total_yen") if v.get("monthly_total_yen") is not None else v.get("base_salary_yen")
    return {**m, "records": [{**r, "value": val(r.get("value") or {}), "raw": r.get("value") or {}} for r in records(m)]}


BASE_ONLY_PRE, BASE_ONLY_TIP = "基", "月給額の記載がなく基本給のみ"


def salary_fmt(v) -> str:
    return f"{v / 1e4:.1f}"


def salary_parts(v) -> dict:
    """内訳の額（円）。その他 = 月給額 − 基本給 − 固定残業代（月給額と基本給が両方あるときだけ。固定残業代の記載なしは 0 円）。"""
    total, base, fixed = v.get("monthly_total_yen"), v.get("base_salary_yen"), v.get("fixed_overtime_yen")
    other = total - base - (fixed or 0) if total is not None and base is not None else None
    return {"基本給": base, "固定残業代": fixed, "その他": other}


def salary_part_text(v, name) -> str:
    """内訳列のセル・候補の表示。固定残業代は時間数があれば「4.1万円/20時間」。額がなければ —。"""
    x = salary_parts(v)[name]
    if x is None:
        return MISSING
    hours = v.get("fixed_overtime_hours")
    return salary_fmt(x) + ("万円/" + f"{hours:g}時間" if name == "固定残業代" and hours is not None else "")


def salary_cells(slots, key) -> list:
    """1学位分の [月給額, 基本給, 固定残業代, その他] のセル。すべて同じ記録（優先順位の先頭）から作る。"""
    t = top(tiers_for(slots, lambda s: records(salary_metric(s, key))))
    if not t:
        return [cell(MISSING) for _ in range(4)]
    (rec, marks), v = t, t[0]["raw"]
    if v.get("monthly_total_yen") is None:
        out = [cell(salary_fmt(rec["value"]), rec["value"], marks, BASE_ONLY_TIP, BASE_ONLY_PRE)]
    else:
        out = [cell(salary_fmt(rec["value"]), rec["value"], marks)]
    for name in SALARY_PARTS:
        x = salary_parts(v)[name]
        full = salary_part_text(v, name)
        out.append(cell(MISSING) if x is None else cell(salary_fmt(x), x, marks, full if full != salary_fmt(x) else ""))
    return out


def max_salary_cell(degree_cells) -> list:
    """学位別の月給額セルのうち最大のもの（規約は SALARY_KEYS の注釈）。印・ソートキー・title はそのセルのもの。"""
    best = None
    for i, c in enumerate(degree_cells):
        if c[1] is not None and (best is None or c[1] > degree_cells[best][1]):
            best = i
    if best is None:
        return cell(MISSING), None
    c = degree_cells[best]
    return cell(c[0], c[1], c[2], BASE_ONLY_TIP if c[4:5] == [BASE_ONLY_PRE] else "", DEGREES[best][0]), best


def salary_text(v) -> str:
    """候補ポップアップ用。セルに出す額に、基本給・固定残業代の内訳があれば添える（基本給だけなら基本給と明示）。"""
    total, base = v.get("monthly_total_yen"), v.get("base_salary_yen")
    if total is None:
        return f"基本給 {salary_fmt(base)}万円"
    parts = []
    if base is not None:
        parts.append(f"基本給 {salary_fmt(base)}万円")
    if v.get("fixed_overtime_yen") is not None:
        hours = v.get("fixed_overtime_hours")
        parts.append(f"固定残業代 {salary_fmt(v['fixed_overtime_yen'])}万円" + (f"/{hours:g}時間" if hours is not None else ""))
    elif v.get("fixed_overtime_hours") is not None:
        parts.append(f"固定残業 {v['fixed_overtime_hours']:g}時間")
    return f"{salary_fmt(total)}万円" + (f"（{'・'.join(parts)}）" if parts else "")


def detail(title, slots) -> dict:
    """右パネル用の詳細（選考工程と募集回）。"""
    proc = pick(tiers_for(slots, lambda s: records((s.get("selection_process") or {}).get("process"))),
                lambda v: v)
    process = None
    if proc[0] != MISSING:
        process = {"marks": proc[2], "stages": [
            [st.get("label", "") for st in (stage.get("steps") or [])]
            for stage in (proc[0].get("stages") or [])]}
    windows, wmarks = [], ""
    for kind in KIND_ORDER:
        for slot, prev in slots:
            ws = [w for w in (slot.get("application_windows") or {}).get("records") or [] if kind_of(w) == kind]
            if ws:
                wmarks = ("前" if prev else "") + ("非" if kind != "official" else "")
                for w in ws:
                    j = lambda m, f=fmt_date_range: "・".join(dict.fromkeys(f(r["value"]) for r in records(m) if r.get("value"))) or MISSING
                    win = {"name": w.get("window_name") or MISSING,
                           "open": j(w.get("application_open_date")),
                           "deadline": j(w.get("application_deadline_date"))}
                    if prev:  # 前年度の枠の日付: 「前年度の日程を+1年」用に、ずらした表示文字列も持つ
                        sh = lambda v: shifted(v)[0]
                        win["open1"], win["deadline1"] = j(w.get("application_open_date"), sh), j(w.get("application_deadline_date"), sh)
                    windows.append(win)
                break
        if windows:
            break
    return {"title": title, "process": process, "windows": windows, "wmarks": wmarks}


INDUSTRY_FIELD = "industry_classification_codes"


# 略称が公式名と離れている大分類は、ツールチップに公式名を添える。
JSIC_FULL = {"専門サービス": "学術研究，専門・技術サービス業"}


def industry_cells(recs) -> list:
    """業種の4セル: 畳んだ列（先頭＝主な事業。全件はツールチップ）と 1位〜3位。印はどれも同じ記録のもの。"""
    t = top([(recs, False)])
    if not t:
        return [cell(MISSING)] * (1 + len(INDUSTRY_POS))
    v, marks = t[0]["value"], t[1]
    codes = v if isinstance(v, list) else [v]
    name = lambda c: INDUSTRY_LABELS.get(c, str(c))
    full = lambda c: name(c) + (f"（{JSIC_FULL[name(c)]}）" if name(c) in JSIC_FULL else "")
    tip = "/".join(full(c) for c in codes)
    first = cell(name(codes[0]), codes[0], marks, tip=tip if tip != name(codes[0]) else "")
    return [first] + [cell(name(codes[n]), codes[n], marks, tip=full(codes[n]) if full(codes[n]) != name(codes[n]) else "")
                      if n < len(codes) else cell(MISSING) for n in range(len(INDUSTRY_POS))]


def profile_cells(profile) -> list:
    out = []
    info = (profile or {}).get("basic_information") or {}
    for field, _label, fmt in PROFILE_COLUMNS:
        if profile is None:
            out.extend([cell(MISSING)] * (1 + (len(INDUSTRY_POS) if field == INDUSTRY_FIELD else 0)))
            continue
        if field == INDUSTRY_FIELD:
            out.extend(industry_cells(records(info.get(field))))
            continue
        f = fmt or fmt_bool
        out.append(pick([(records(info.get(field)), False)], num_fmt(f),
                        ordinal_key(field) if field in ORDINAL
                        else first_code_key if field == "industry_classification_codes" else None))
    return out


def profile_details(profile) -> dict:
    """企業情報の列番号 -> セル詳細。列の位置は COLUMNS 上のもの。"""
    info = (profile or {}).get("basic_information") or {}
    out = {}
    for field, label, fmt in PROFILE_COLUMNS:
        m = info.get(field)
        d = pack(metric_cands([(records(m), 0)], num_fmt(fmt)), metric_notes([(0, m)]))
        if d:
            out[COL_INDEX[label]] = d
            if field == INDUSTRY_FIELD:
                out.update({COL_INDEX[c]: d for c in INDUSTRY_POS})
    return out


def company_name(profiles, cid) -> str:
    return (profiles.get(cid) or {}).get("legal_name") or cid


def name_cell(full: str) -> list:
    short = short_name(full)
    return cell(short, short, "", full)


def slot_info(profiles, case, base, name, prev_only, cid, year) -> dict:
    """応募枠セルのポップアップ用。この行の配属先（正式名）と、同じ枠の他の配属先、枠レベルの出典・注記。"""
    ids = ((base.get("assignment_companies") or {}).get("company_ids")) or []
    inv = case.get("application_slot_inventory") or {}
    return {
        "name": name, "year": base.get("application_year"), "prev": prev_only, "ry": year,
        "assign": company_name(profiles, cid) if cid else None,
        "others": [company_name(profiles, i) for i in ids if i != cid],
        "src": [[r.get("application_year"), (r.get("source") or {}).get("url") or ""]
                for r in inv.get("records") or [] if r.get("slot_name") == name],
        "note": (inv.get("note") or "").strip(),
    }


def load_slot_short_names() -> dict[str, dict[str, str]]:
    """data/slot_short_names.json（{ファイル名の幹: {正式な枠名: 短い名前}}）。表示用の設定で、調査データではない。"""
    path = DATA_DIR / "slot_short_names.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def build_rows(profiles: dict[str, dict], cases: list[dict]):
    """(行, 応募枠グループごとのセル詳細, 企業ごとのセル詳細) を返す。行は詳細への添字/IDだけを持つ。"""
    rows, groups, companies = [], [], {}
    slot_shorts = load_slot_short_names()
    for case in cases:
        year = case.get("recruitment_year")
        pub = case.get("publishing_company_id")
        shorts = slot_shorts.get(f"{pub}_{year}", {})
        by_name: dict[str, dict] = {}
        for slot in case.get("application_slots") or []:
            ay = slot.get("application_year")
            if ay not in (year, year - 1):
                continue
            by_name.setdefault(slot["slot_name"], {})["cur" if ay == year else "prev"] = slot
        for name, g in by_name.items():
            prev_only = "cur" not in g
            # 前年度のみの枠も、値のセルごとに「前」を付ける（応募枠名の「前」だけでは横に読んだとき気づきにくい）。
            slots = [(g[k], k == "prev") for k in ("cur", "prev") if k in g]
            base = g.get("cur") or g["prev"]
            ids = ((base.get("assignment_companies") or {}).get("company_ids")) or []
            targets = ids or [None]
            pub_name = company_name(profiles, pub)
            short = shorts.get(name, name)
            pub_cell = name_cell(pub_name)
            common = [cell(short, short, "前" if prev_only else "", name if short != name else "")]
            tail = [
                window_date_cell(slots, "application_open_date"), window_date_cell(slots, "application_deadline_date"),
                *selection_cells(slots),
            ]
            ds = {COL_INDEX["最初の募集開始"]: window_date_detail(slots, "application_open_date", year),
                  COL_INDEX["最初の締切"]: window_date_detail(slots, "application_deadline_date", year),
                  COL_INDEX["選考"]: selection_detail(slots, year)}
            per_degree = []
            for deg in DEGREES:
                key = SALARY_KEYS[deg]
                per_degree.append(salary_cells(slots, key))
                # 内訳列も、月給額と同じ候補一覧（同じ記録に「採用」印）に、その列の値を見せる。
                ms = [(ycode(s, year), salary_metric(s, key)) for s, _ in slots]
                for n, part in enumerate(("初任給", *SALARY_PARTS)):
                    fmt = salary_text if n == 0 else (lambda v, nm=part: salary_part_text(v, nm))
                    ds[COL_INDEX[deg + part]] = pack(metric_cands(
                        [([{**r, "value": r["raw"]} for r in records(m)], y) for y, m in ms], fmt),
                        metric_notes([(ycode(s, year), s.get(key)) for s, _ in slots]))
            best_cell, best = max_salary_cell([c[0] for c in per_degree])
            tail.append(best_cell)
            if best is not None:
                ds[COL_INDEX["初任給"]] = ds[COL_INDEX[DEGREES[best] + "初任給"]]
            tail += [c for cells in per_degree for c in cells]
            # 枠のリモート勤務は多くが企業全体の制度からの流用で「リモ制」と重なるため、列にせず応募枠のポップアップに出す。
            ds["remote"] = metric_detail(slots, lambda s: s.get("has_remote_work"), fmt_bool, year)
            gid = len(groups)
            groups.append({str(k): v for k, v in ds.items() if v})
            for cid in targets:
                pid = pub if cid is None else cid
                prof = profiles.get(pid)
                assign_name = company_name(profiles, pid)
                if pid not in companies:
                    companies[pid] = {str(k): v for k, v in profile_details(prof).items()}
                d = detail([assign_name, name], slots)
                if pid != pub:
                    d["pub"] = pub_name
                d["g"], d["p"] = gid, pid
                d["slot"] = slot_info(profiles, case, base, name, prev_only, cid, year)
                rows.append([name_cell(assign_name), *common] + tail + profile_cells(prof) + [pub_cell, d])
    return rows, groups, {k: v for k, v in companies.items() if v}


def column_descriptions() -> dict[str, dict]:
    """列名 -> {"short": 読み手向けの短い説明, "definition": 調査での定義の段落}。

    定義の正本は schemas/ の description（調査エージェント向けの記録規則）で、そのまま折りたたんで添える。
    short は表の読み方だけを書き、定義を言い換えて重複させない。
    """
    slot, win, part = ApplicationSlot.model_fields, ApplicationWindow.model_fields, StartingSalary.model_fields
    sel = SelectionProcess.model_fields
    short = {
        "配属先": "入社後に配属される企業。右側の企業情報はこの企業のもの。配属先が複数ある枠は配属先ごとに1行。",
        "採用元": "募集を出している企業（応募窓口）。",
        "応募枠": "応募のコース・職種。クリックで配属先・リモート勤務・出典を表示。",
        "最初の募集開始": "全募集回のうち最も早い応募開始日。",
        "最初の締切": "全募集回のうち最も早い締切日。",
        "初任給": "学士・修士・博士のうち最も高い月給。左の小さな字が学位。▸ で学位別に展開。",
        "選考": "多くの枠にはない選考のうち、あるもの。G=GD 集=集団面接 ケ=ケース 課=課題 研=研究発表 コ=コーディング。▸ で全10種。",
        "業種": "主な事業（売上最大）の業種。▸ で売上の大きい順に最大3つを列に展開。",
        **{p: f"売上が{n}番目に大きい事業の業種（なければ —）。" for n, p in zip("大二三", INDUSTRY_POS)},
        "平均年収": "従業員の平均年収。",
        "月残業": "従業員の月平均残業時間。",
        "年間休日": "年間休日の日数。",
        "有給取得": "有給休暇の年間平均取得日数。",
        "リモート制度": "会社のリモート勤務制度（なし < 一部 < 併用 < フル）。",
        "3年離職率": "新卒入社者が3年以内に辞めた割合。",
        "平均勤続": "従業員の平均勤続年数。",
        "平均年齢": "従業員の平均年齢。",
        "えるぼし": "女性活躍推進の国の認定。1〜3段階と、最上位のプラチナ。",
        "従業員数": "従業員数（原則その会社単体）。",
        "資本金": "資本金。",
        "研究開発費": "年間の研究開発費。",
        "設立": "設立年。",
    }
    definition = {label: [BasicInformation.model_fields[field].description] for field, label, _ in PROFILE_COLUMNS}
    for p in INDUSTRY_POS:
        definition[p] = definition["業種"]
    definition.update({
        "配属先": [],
        "採用元": [],
        "応募枠": ["リモート勤務：" + slot["has_remote_work"].description],
        "最初の募集開始": [win["application_open_date"].description],
        "最初の締切": [win["application_deadline_date"].description],
        "初任給": [],
        "選考": [f"{label}：{sel[f].description}" for f, label in SELECTION_FLAGS],
    })
    web = {"ES": "ES提出", "Web": "Webテスト", "筆記": "筆記試験", "課題": "課題提出", "GD": "GD", "集団面接": "集団面接",
           "面接": "面接", "ケース": "ケース面接", "研究発表": "研究発表", "コーディング": "コーディング試験"}
    for field, label in SELECTION_FLAGS:
        short[label] = f"{web[label]}があるか（○あり ×なし —不明）。"
        definition[label] = [sel[field].description]
    for deg, key in SALARY_KEYS.items():
        short[deg + "初任給"] = f"{deg}の月給額（固定残業代・固定手当を含む総額）。基本給しかなければ「基」付きで基本給。▸ で内訳。"
        short[deg + "基本給"] = f"{deg}の基本給（sourceが基本給として明示する月額）。"
        short[deg + "固定残業代"] = f"{deg}の月給額に含まれる固定残業代（みなし残業代）。セルをクリックすると対応する時間も表示。"
        short[deg + "その他"] = f"{deg}の月給額 − 基本給 − 固定残業代（調整手当などの固定手当）。月給額と基本給が両方分かるときだけ表示。"
        definition[deg + "初任給"] = [slot[key].description]
        definition[deg + "基本給"] = [part["base_salary_yen"].description]
        definition[deg + "固定残業代"] = [part["fixed_overtime_yen"].description]
        definition[deg + "その他"] = []
    return {c: {"short": short[c], "definition": definition[c]} for c in short}


def load_dir(path: Path, model_class) -> tuple[list[dict], list[Path]]:
    """path 内の JSON を読み、現行 schema を満たすものだけ返す。(読めたもの, 読み飛ばしたファイル)"""
    loaded, skipped = [], []
    for f in sorted(path.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            model_class.model_validate(data)
        except (ValueError, OSError):
            skipped.append(f)
            continue
        loaded.append(data)
    return loaded, skipped


def fold_role(col) -> list:
    """折りたたみ上の役割 [種別, 学位の番号]。種別: ""=常に表示 / max=初任給(畳んだとき) / deg=学位別の月給額 / sub=内訳。"""
    if col == "初任給":
        return ["max", -1]
    for k, deg in enumerate(DEGREES):
        if col == deg + "初任給":
            return ["deg", k]
        if col in (deg + n for n in SALARY_PARTS):
            return ["sub", k]
    if col == "業種":
        return ["imax", -1]
    if col in INDUSTRY_POS:
        return ["isub", -1]
    if col == "選考":
        return ["smax", -1]
    if col in SELECTION_LABELS:
        return ["ssub", -1]
    return ["", -1]


def header_path(col) -> list:
    """多段見出しでの祖先グループ（上位から）。学位別の列は内訳を開いたとき JS が学位のグループを足す。"""
    if col in ("配属先", "応募枠"):
        return ["募集"]
    if col in ("最初の募集開始", "最初の締切"):
        return ["日程"]
    if col in SELECTION_LABELS:
        return ["選考"]
    role, k = fold_role(col)
    if role == "deg":
        return ["初任給"]
    if role == "sub":
        return ["初任給", DEGREES[k]]
    if col in INDUSTRY_POS:
        return ["企業情報", "業種"]
    if col in {c[1] for c in PROFILE_COLUMNS}:
        return ["企業情報"]
    return []


def encrypt_payload(plaintext: bytes, password: str) -> dict:
    """PBKDF2-HMAC-SHA256 で鍵を導出し AES-256-GCM で暗号化する。"""
    if not password:
        raise ValueError(f"{PASSWORD_ENV} is required.")
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "alg": "AES-256-GCM", "kdf": "PBKDF2", "hash": "SHA-256",
            "iterations": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _decimals(text) -> int:
    m = _NUM.fullmatch(str(text))
    return len(m[0].partition(".")[2].rstrip("0")) if m else 0


def _pad(text, digits: int) -> str:
    """数値だけの文字列、または「…万円」の数値の末尾のゼロを落とし、小数 digits 桁までゼロ埋めする。"""
    def one(m):
        head, _, frac = m[0].partition(".")
        frac = frac.rstrip("0").ljust(digits, "0")
        return head + "." + frac if frac else head
    t = str(text)
    return one(_NUM.fullmatch(t)) if _NUM.fullmatch(t) else re.sub(r"-?\d[\d,]*(?:\.\d+)?(?=万円)", one, t)


def unify_decimals(rows, groups, companies) -> None:
    """数値列ごとに、表示される小数の桁数の最大に全セル・候補・ツールチップをそろえる（整数だけの列は小数なし）。
    ソートキーは変えない。基準は表のセル（候補の値は丸めの上限が同じなので、足りない桁をゼロ埋めするだけ）。"""
    for i, name in enumerate(COLUMNS):
        if name not in NUMERIC_COLUMNS:
            continue
        digits = max((_decimals(r[i][0]) for r in rows), default=0)
        for r in rows:
            c = r[i]
            c[0] = _pad(c[0], digits)
            if len(c) > 3 and c[3]:
                c[3] = _pad(c[3], digits)
        for store in (*groups, *companies.values()):
            cd = store.get(str(i)) or store.get(i)
            for cand in (cd[0] if cd else []):
                cand[0] = _pad(cand[0], digits)


def build_html(rows: list[list], groups: list[dict], companies: dict[str, dict], skipped: int = 0,
               password: str | None = None) -> str:
    """password が None または空なら平文、非空なら暗号化した閲覧ページを返す。環境変数は読まない。"""
    unify_decimals(rows, groups, companies)
    jst = timezone(timedelta(hours=9))
    urls: dict[str, int] = {}
    for store in [*groups, *companies.values()]:
        for cd in store.values():
            for c in cd[0]:
                c[3] = urls.setdefault(c[3], len(urls)) if c[3] else -1
    notes: dict[str, int] = {}
    for store in [*groups, *companies.values()]:
        for cd in store.values():
            for n in cd[1]:
                n[1] = notes.setdefault(n[1], len(notes))
    desc = column_descriptions()
    payload = {
        "urls": list(urls), "notes": list(notes),
        "columns": COLUMNS, "headers": [SHORT.get(c, c) for c in COLUMNS],
        "full": [f"{c}（{UNITS[c]}）" if c in UNITS else c for c in COLUMNS],
        "desc": [desc[c] for c in COLUMNS], "numeric": sorted(NUMERIC_COLUMNS),
        "rows": rows,
        "fold": [fold_role(c) for c in COLUMNS], "hpath": [header_path(c) for c in COLUMNS],
        "groups": groups, "companies": companies,
        "skipped": skipped,
        "built_at": datetime.now(jst).strftime("%Y-%m-%d %H:%M JST"),
    }
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if password:
        data = json.dumps(encrypt_payload(plain, password), separators=(",", ":"))
        parts = {"__LOCK_CSS__": LOCK_CSS, "__LOCK_HTML__": LOCK_HTML, "__APP_HIDDEN__": " hidden", "__BOOT__": DECRYPT_JS}
    else:
        data = plain.decode("utf-8").replace("<", "\\u003c")  # </script> や <!-- で script 要素が閉じないようにする
        parts = {"__LOCK_CSS__": "", "__LOCK_HTML__": "", "__APP_HIDDEN__": "", "__BOOT__": PLAIN_JS}
    out = TEMPLATE
    for k, v in parts.items():
        out = out.replace(k, v)
    return out.replace("__DATA__", data)  # データは最後に置換する（データ内の記号を誤置換しない）


def is_git_ignored(path: Path) -> bool:
    """path が WORKSPACE_ROOT の git で ignore されていれば True。リポジトリ外や git の失敗は False。"""
    try:
        r = subprocess.run(["git", "-C", str(WORKSPACE_ROOT), "check-ignore", "-q", str(path.resolve())],
                           capture_output=True)
    except OSError:
        return False
    return r.returncode == 0




def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="公開用の暗号化 HTML（REPORT_ACCESS_PASSWORD 設定時のみ生成）")
    parser.add_argument("--plain-output", type=Path, default=DEFAULT_PLAIN_OUTPUT, help="ローカル確認用の平文 HTML（毎回生成。git 管理外のパスのみ可）")
    args = parser.parse_args(argv)
    if not is_git_ignored(args.plain_output):
        print(f"エラー: 平文の出力先 {args.plain_output} は git の管理外（.gitignore）ではないか、リポジトリ外にあります。"
              "平文データを誤ってコミットしないよう、何も書き込まずに終了します。", file=sys.stderr)
        return 1
    password = os.environ.get(PASSWORD_ENV, "")
    profile_list, skipped = load_dir(DATA_DIR / "companies", CompanyProfile)
    case_list, skipped_cases = load_dir(DATA_DIR / "recruitment_cases", RecruitmentCase)
    skipped += skipped_cases
    if skipped:
        print(f"現行 schema を満たさないため {len(skipped)} 件を読み飛ばした:", file=sys.stderr)
        for f in skipped:
            print(f"  {f}", file=sys.stderr)
    profiles = {p["company_id"]: p for p in profile_list}
    rows, groups, companies = build_rows(profiles, case_list)
    args.plain_output.parent.mkdir(parents=True, exist_ok=True)
    args.plain_output.write_text(build_html(rows, groups, companies, len(skipped)), encoding="utf-8")
    print(f"平文: {args.plain_output.resolve()} ({len(rows)} 行)")
    if password:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(build_html(rows, groups, companies, len(skipped), password=password), encoding="utf-8")
        print(f"暗号化: {args.output.resolve()} ({len(rows)} 行)")
    else:
        print(f"{PASSWORD_ENV} が未設定または空のため、平文のみ生成した（暗号化版は未生成）。", file=sys.stderr)
    return 0


LOCK_CSS = r"""#lock{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
#lock[hidden]{display:none}
#lock form{width:340px;max-width:100%;padding:24px;border:1px solid var(--line);border-radius:12px;background:var(--surface);box-shadow:var(--shadow)}
#lock h1{font-size:20px;margin-bottom:4px}
#lock p{margin:0 0 14px;color:var(--mut);font-size:13px}
#lock input{width:100%;padding:9px 12px;font:inherit;font-size:16px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--fg);outline:none}
#lock input:focus{border-color:var(--hl);box-shadow:0 0 0 3px color-mix(in srgb,var(--hl) 22%,transparent)}
#lock button{width:100%;margin-top:10px;padding:9px 12px;font:inherit;font-weight:650;border:0;border-radius:8px;background:var(--hl);color:var(--hlfg);cursor:pointer}
#lock button:disabled{opacity:.6;cursor:wait}
#lockerr{min-height:20px;margin-top:8px;font-size:13px;color:var(--warn)}
"""

LOCK_HTML = r"""<div id="lock"><form id="lockform" autocomplete="off"><h1>企業調査一覧</h1><p>閲覧には合言葉が必要です。</p>
<input id="pw" type="password" placeholder="合言葉" autocomplete="current-password" aria-label="合言葉" autofocus>
<div id="lockerr" role="alert"></div><button type="submit" id="lockbtn">開く</button></form></div>
"""

DECRYPT_JS = r"""// --- 復号：PBKDF2-HMAC-SHA256 → AES-256-GCM（既存の暗号化ビューアと互換）。導出した鍵のバイト列だけを sessionStorage に保持する ---
const E=JSON.parse(document.getElementById('data').textContent);
const SK='viewerKey',b2u=v=>Uint8Array.from(atob(v),c=>c.charCodeAt(0));
const u2b=u=>btoa(String.fromCharCode(...u));
async function decryptWith(keyBytes){
 const k=await crypto.subtle.importKey('raw',keyBytes,'AES-GCM',false,['decrypt']);
 const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv:b2u(E.iv)},k,b2u(E.ct));
 return JSON.parse(new TextDecoder().decode(pt))}
async function deriveBytes(pw){
 const m=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveBits']);
 return new Uint8Array(await crypto.subtle.deriveBits({name:'PBKDF2',salt:b2u(E.salt),iterations:Number(E.iterations),hash:E.hash},m,256))}
function show(D){document.getElementById('lock').hidden=true;document.getElementById('app').hidden=false;start(D)}
(async()=>{
 const lock=document.getElementById('lock'),form=document.getElementById('lockform'),pw=document.getElementById('pw'),err=document.getElementById('lockerr'),btn=document.getElementById('lockbtn');
 if(!(window.crypto&&crypto.subtle)){err.textContent='復号には HTTPS またはローカルHTTPサーバー（localhost）が必要です。';btn.disabled=true;return}
 try{const s=sessionStorage.getItem(SK);if(s){try{show(await decryptWith(b2u(s)));return}catch(e){sessionStorage.removeItem(SK)}}}catch(e){}
 form.addEventListener('submit',async ev=>{
  ev.preventDefault();err.textContent='';btn.disabled=true;
  let D,bytes;
  try{bytes=await deriveBytes(pw.value.trim());D=await decryptWith(bytes)}
  catch(e){err.textContent='合言葉が違います。';btn.disabled=false;pw.select();pw.focus();return}
  try{sessionStorage.setItem(SK,u2b(bytes))}catch(e){}
  pw.value='';show(D)});
 pw.focus()})();
"""

PLAIN_JS = "const D=JSON.parse(document.getElementById('data').textContent);\nstart(D);\n"

TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>企業調査一覧</title>
<style>
:root{--bg:#f7f8fa;--surface:#fff;--fg:#1f2933;--mut:#667085;--line:#e4e8ee;--line2:#c9d1db;--head:#eef2f6;--hover:#f3f7fa;--hl:#1f6f5b;--hlfg:#fff;--soft:#e4f3ee;--soft-line:#b7ded4;--link:#155eef;--warn:#9a3412;--warn-bg:#fff3e8;--warn-line:#fed7aa;--sel:#e4f3ee;--prev-fg:#0369a1;--badge:#eef2f6;--badgefg:#475467;--card:0 1px 2px rgba(16,24,40,.05);--shadow:0 10px 30px rgba(16,24,40,.16),0 1px 3px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#101317;--surface:#181c21;--fg:#e6e9ed;--mut:#98a2b3;--line:#262c34;--line2:#39414c;--head:#1f242b;--hover:#1e242b;--hl:#5fc2a5;--hlfg:#0f1a16;--soft:#1b3a31;--soft-line:#2a5a4b;--link:#8ab4ff;--warn:#fdba74;--warn-bg:#3a2a1c;--warn-line:#6b4423;--sel:#1b3a31;--prev-fg:#7dd3fc;--badge:#262c34;--badgefg:#b6bfcb;--card:none;--shadow:0 10px 30px rgba(0,0,0,.55),0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box}
__LOCK_CSS__
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Hiragino Sans","Noto Sans JP",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.app{max-width:1840px;margin:0 auto;padding:14px 16px 16px;height:100vh;height:100dvh;display:flex;flex-direction:column}
.app[hidden]{display:none}
.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:8px 20px;margin-bottom:8px}
h1{font-size:24px;line-height:1.3;font-weight:700;margin:0 0 2px}
.sub{color:var(--mut);font-size:13px}
.meta{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
#info{margin-top:4px}
.tools{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px}
.tgl{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--fg);cursor:pointer;user-select:none}
.tgl input{accent-color:var(--hl);width:15px;height:15px;margin:0;cursor:pointer}
.sh{font-style:italic}
td.pv,.legend .mk-p{color:var(--prev-fg)}
.mk-u{text-decoration:underline dotted;text-decoration-color:var(--warn);text-decoration-thickness:1.5px;text-underline-offset:3px}
.mk-m{position:relative}
.mk-m::after{content:"";position:absolute;top:0;right:-5px;width:4px;height:4px;border-radius:50%;background:var(--hl)}
td.c1 .mk-m::after{display:none}
#q{width:300px;max-width:100%;padding:9px 12px;font:inherit;font-size:16px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--fg);outline:none;box-shadow:var(--card)}
#q:focus{border-color:var(--hl);box-shadow:0 0 0 3px color-mix(in srgb,var(--hl) 22%,transparent)}
details.legend{margin:0 0 8px;padding:6px 12px;border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--card)}
details.legend summary{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;cursor:pointer;list-style:none;font-size:12.5px;color:var(--mut);user-select:none}
details.legend summary::-webkit-details-marker{display:none}
details.legend summary .more{margin-left:auto;color:var(--link)}
details.legend summary .more::before{content:"▸ "}details.legend[open] summary .more::before{content:"▾ "}
details.legend .help{margin:8px 0 2px;padding-top:8px;border-top:1px solid var(--line);line-height:1.8;max-width:1100px;color:var(--mut);font-size:12.5px}
:root{--name-w:150px}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 400px;grid-template-rows:minmax(0,1fr);gap:16px;flex:1;min-height:0}
.wrap{overflow:auto;min-height:0;border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--card)}
#panel{box-sizing:border-box;min-height:0;overflow:auto;padding:18px;border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--card);overflow-wrap:anywhere}
#panel h2{font-size:20px;line-height:1.3;margin:0}
#panel .psub{margin-top:3px;color:var(--mut);font-size:13px}
#panel h3{display:flex;align-items:center;gap:6px;font-size:14px;margin:18px 0 0;padding-top:14px;border-top:1px solid var(--line)}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:14px}
.stat{min-width:0;padding:8px 10px;border:1px solid var(--line);border-radius:8px}
.stat .l{color:var(--mut);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat .v{font-size:17px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
.stat.big{grid-column:1/-1;border-color:var(--soft-line);background:var(--soft)}
.stat.big .v{font-size:28px;line-height:1.2;color:var(--hl)}
.stat .v .u{font-size:12px;font-weight:500;color:var(--mut);margin-left:3px}
.stat .v .pre{font-size:12px;font-weight:600;color:var(--hl)}
.facts{list-style:none;margin:8px 0 0;padding:0}
.facts li{display:grid;grid-template-columns:150px minmax(0,1fr);gap:8px;padding:4px 6px;margin:0 -6px;border-bottom:1px solid var(--line);border-radius:6px}
.facts li:last-child{border-bottom:0}
.facts li.hasd{cursor:pointer}.facts li.hasd:hover{background:var(--hover)}
.facts .l{color:var(--mut)}
.facts .v{font-variant-numeric:tabular-nums}
.steps{list-style:none;margin:10px 0 0;padding:0;counter-reset:st}
.steps li{display:flex;gap:8px;align-items:baseline;margin-bottom:6px}
.steps li::before{counter-increment:st;content:counter(st);flex:none;width:20px;height:20px;border-radius:50%;background:var(--soft);color:var(--hl);font-size:11px;font-weight:700;line-height:20px;text-align:center}
.win{margin-top:8px;padding:10px;border:1px solid var(--line);border-radius:8px}
.win .n{font-weight:650;line-height:1.4}
.win .d{display:flex;flex-wrap:wrap;gap:6px 12px;margin-top:6px;font-size:12px;color:var(--mut)}
.dt{display:inline-block;margin-left:4px;padding:2px 8px;border-radius:6px;background:var(--soft);color:var(--hl);font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.dt.e{background:var(--warn-bg);color:var(--warn);font-weight:600}
.k{color:var(--mut);font-size:11px;margin-left:4px}
tr.sel td{background:var(--sel)!important}
tr.sel td.selc{outline:2px solid var(--hl);outline-offset:-2px}
#pop{position:fixed;z-index:40;width:360px;max-width:calc(100vw - 16px);max-height:70vh;overflow:auto;overflow-wrap:anywhere;padding:14px 16px;background:var(--surface);color:var(--fg);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);font-size:13px;line-height:1.6;white-space:normal;text-align:left}
#pop[hidden]{display:none}
#pop .ph{display:flex;gap:8px;align-items:flex-start;font-size:14px;font-weight:700;line-height:1.4;margin-bottom:10px}
#pop .ph span{flex:1}
#pop .x{flex:none;width:24px;height:24px;margin:-3px -6px 0 0;border:0;border-radius:6px;background:none;color:var(--mut);font-size:14px;line-height:1;cursor:pointer}
#pop .x:hover{background:var(--hover);color:var(--fg)}
#pop ul{margin:0;padding-left:18px}#pop li{margin-bottom:6px}#pop li::marker{color:var(--line2)}
#pop p{margin:0 0 8px}#pop .def{margin-top:4px;color:var(--mut);font-size:12px}#pop .def summary{cursor:pointer;margin-bottom:6px}#pop .note{white-space:pre-wrap;font-size:12px;color:var(--fg)}
#pop .ok{display:inline-block;margin-left:6px;padding:0 6px;border-radius:999px;background:var(--hl);color:var(--hlfg);font-size:10px;line-height:16px;vertical-align:middle}
#pop .sort{display:flex;gap:6px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
#pop .sort button{padding:4px 12px;border:1px solid var(--line2);border-radius:999px;background:var(--surface);color:var(--fg);font:inherit;font-size:12px;cursor:pointer}
#pop .sort button:hover{background:var(--hover)}
#pop .sort button.on{background:var(--hl);color:var(--hlfg);border-color:var(--hl)}
table{border-collapse:separate;border-spacing:0;white-space:nowrap;font-size:13.5px}
th,td{padding:7px 12px;border-bottom:1px solid var(--line);background:var(--surface);text-align:left}
tbody tr{cursor:pointer}
tbody tr:hover td{background:var(--hover)}
tbody tr:last-child td{border-bottom:0}
th{position:sticky;top:0;z-index:2;background:var(--head);cursor:pointer;user-select:none;color:var(--fg);font-size:12.5px;font-weight:650;padding:9px 12px;border-bottom:1px solid var(--line2)}
th:hover{color:var(--hl)}
th.sorted{color:var(--hl)}
thead tr{height:38px}thead th{box-sizing:border-box;height:38px;padding-top:0;padding-bottom:0;line-height:16px}thead tr:last-child,thead tr:last-child th{height:46px}
th.gh{text-align:center;background:var(--head);color:var(--mut);border-left:1px solid var(--line2);border-right:1px solid var(--line2)}
th.gh:hover{color:var(--hl)}
th.g0:first-child{left:0;z-index:3;border-left:0}
.gs{border-left:1px solid var(--line2)}
th.sorted .lb::after{content:attr(data-dir);margin-left:4px;font-size:10px}
.tg{position:relative;display:flex;align-items:center;justify-content:center;box-sizing:border-box;width:calc(100% + 12px);height:16px;margin:2px -6px 0;padding:0;border:1px solid var(--line2);border-radius:6px;background:var(--surface);color:var(--fg);font:inherit;font-size:10px;line-height:1;cursor:pointer}
.tg::after{content:"";position:absolute;left:0;right:0;top:-4px;bottom:-4px}
.tg:hover,.tg:focus-visible{background:var(--soft);border-color:var(--hl);color:var(--hl)}
.pre{display:inline-block;width:1.1em;margin-right:3px;text-align:left;color:var(--mut);font-size:11px}
td.num{text-align:right;font-variant-numeric:tabular-nums}
th.num{text-align:right}
/* 右寄せの数値の後ろに印を置くと桁がずれるので、印の幅を数値列のすべてのセルで同じだけ確保する（幅は列ごとに JS で設定） */
td.num .bs{display:inline-block;text-align:left;white-space:nowrap}
.c0,.c1{position:sticky;z-index:1}
th.c0,th.c1{z-index:3}
.c0{left:0;min-width:var(--name-w);max-width:var(--name-w);overflow:hidden;text-overflow:ellipsis;font-weight:600;color:var(--link)}
tr.sel .c0{color:var(--hl)}
.c1{left:var(--name-w);min-width:160px;max-width:160px;overflow:hidden;text-overflow:ellipsis;border-right:1px solid var(--line2)}
th.c0{color:var(--fg)}
.pubc{min-width:var(--name-w);max-width:var(--name-w);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.b{display:inline-block;box-sizing:content-box;width:13px;text-align:center;margin-left:3px;padding:0 3px;border:1px solid var(--line2);border-radius:999px;background:var(--badge);color:var(--badgefg);font-size:10px;font-weight:600;line-height:14px;vertical-align:middle}
.b.u{border-color:var(--warn-line);background:var(--warn-bg);color:var(--warn)}
.b.m{border-color:var(--soft-line);background:var(--soft);color:var(--hl)}
.na{color:var(--mut);opacity:.7}
.legend .mk-d{color:var(--mut)}td.dit{color:var(--mut)}td.dit span.dit{opacity:.9;font-size:14px;font-style:normal}
.empty{color:var(--mut)}
@media (max-width:1180px){.layout{grid-template-columns:minmax(0,1fr) 340px}}
@media (max-width:800px){
 #pop{left:0!important;right:0;bottom:0;top:auto!important;width:auto;max-width:none;border-radius:14px 14px 0 0;max-height:60vh}
 .app{padding:10px 10px 16px;height:auto;display:block}h1{font-size:21px}
 .layout{display:block}.wrap{height:calc(100vh - 24px);height:calc(100dvh - 24px);min-height:360px}#panel{height:auto;min-height:0;margin-top:12px;padding:16px}
 .top{display:block}#q{width:100%;margin-top:10px}
 :root{--name-w:104px}.c0{border-right:1px solid var(--line2)}
 .c1{position:static;left:auto;border-right:0;min-width:170px;max-width:170px}
 th.c1{z-index:2}th.g0:first-child{left:auto;z-index:2}
 .facts li{grid-template-columns:132px minmax(0,1fr)}}
</style>
</head>
<body>
__LOCK_HTML__
<div class="app" id="app"__APP_HIDDEN__>
<header class="top">
<div><h1>企業調査一覧</h1><div class="sub">配属先・応募枠ごとの選考、初任給、企業情報の比較</div><div class="meta" id="info"></div></div>
<div class="tools"><label class="tgl" title="前年度の枠から採用した日付の年を1年後ろにずらして表示（今年度の枠の日付は変えない）"><input type="checkbox" id="shift"> 前年度の日程を+1年</label><input id="q" type="search" placeholder="すべての列から検索"></div>
</header>
<details class="legend"><summary><span>値の右の印</span><span><span class="mk mk-p">30.7</span> 前年度の枠から採用</span><span><span class="mk mk-u">30.7</span> 非公式情報</span><span><span class="mk mk-m">30.7</span> 同順位に別の値あり</span><span><span class="mk-d">〃</span> 左の学位と同額</span><span><span class="pre">基</span> 基本給のみ（月給額の記載なし）</span><span class="more">使い方</span></summary>
<div class="help">表の値の装飾：前＝青い字、非＝橙の点線の下線、+＝値の右上の小さな点（複数重なることもある。セルのツールチップに言葉で表示。ポップアップ・右パネルは従来どおり「前」「非」「+」のバッジ）。前：当年度の枠に値がなく、同名の前年度の枠から採用（応募枠名に付く場合は前年度のみの枠）。非：非公式情報（出典区分が不明なものを含む）。+：同順位に異なる値が複数あり先頭を表示。<br>
優先順位：今年度公式 &gt; 前年度公式 &gt; 今年度非公式 &gt; 前年度非公式 &gt; 今年度不明 &gt; 前年度不明（企業情報は公式 &gt; 非公式 &gt; 不明）。「—」は情報なし。「〃」は左の学位（値のある直近のもの）と同額・同じ印で、クリックすると根拠を表示、並べ替えでは値として扱います。列見出しをクリックすると並べ替え（昇順 ▲ → 降順 ▼ → 解除）、重ねると説明を表示（スマホは長押し）、セルをクリックすると値の根拠を表示します。初任給は見出しの ▸ ◂ ▾ で、最大値の1列 ⇄ 学位別3列 ⇄ 内訳（基本給・固定残業代・その他）に切り替えられます。選考は見出しの ▸ ◂ で、まれな選考の記号だけ ⇄ 全10種の○×列に切り替えられます。</div>
</details>
<div class="layout"><div class="wrap"><table><thead id="hd"></thead><tbody id="tb"></tbody></table></div>
<aside id="panel"></aside></div>
</div>
<div id="pop" hidden role="dialog"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
function start(D){
const numeric=new Set(D.numeric),NC=D.columns.length;
let selected=null,selCol=-1,selTd=null,sortCol=-1,sortDir=1,query='';
const hd=document.getElementById('hd'),tb=document.getElementById('tb'),info=document.getElementById('info'),pop=document.getElementById('pop');
const narrow=()=>matchMedia('(max-width:800px)').matches;
function el(t,txt,cls){const e=document.createElement(t);if(txt!==undefined)e.textContent=txt;if(cls)e.className=cls;return e}
// 表のセルの印は幅を取らない文字装飾（前=青い字、非=橙の点線の下線、+=右上の点）。ポップアップ・詳細パネルでは従来のバッジ
const MK={'前':['mk-p','前年度の枠の値'],'非':['mk-u','非公式'],'+':['mk-m','同順位に別の値あり']};
const BC={'非':' u','+':' m'};
function badges(e,m){[...(m||'')].forEach(c=>e.append(el('span',c,'b'+(BC[c]||''))))}
// --- 初任給の折りたたみ：初任給(1列) ⇄ 学位別3列 ⇄ 各学位の内訳。非表示の列は CSS の display:none で隠す（行の列番号は変わらない） ---
const FK='viewerSalaryFold';let fold={e:false,d:[false,false,false],s:false,i:false};
try{const s=JSON.parse(localStorage.getItem(FK));if(s&&typeof s.e==='boolean'&&Array.isArray(s.d))fold={e:s.e,d:[0,1,2].map(k=>!!s.d[k]),s:!!s.s,i:!!s.i}}catch(e){}
const DEGREE_NAMES=['学士','修士','博士'];
const foldStyle=document.head.appendChild(document.createElement('style')),subTg={};
const shown=i=>{const [r,k]=D.fold[i];return r==='max'?!fold.e:r==='deg'?fold.e:r==='sub'?fold.e&&fold.d[k]:r==='smax'?!fold.s:r==='ssub'?fold.s:r==='imax'?!fold.i:r==='isub'?fold.i:true};
function setFold(f){f();try{localStorage.setItem(FK,JSON.stringify(fold))}catch(e){}
 if(sortCol>=0&&!shown(sortCol))sortCol=-1;render()}
function tg(txt,title,f){const t=el('button',txt,'tg');t.type='button';t.title=title;t.setAttribute('aria-expanded',txt==='▾'?'true':'false');t.onclick=e=>{e.stopPropagation();setFold(f)};return t}
function applyFold(){
 foldStyle.textContent=D.columns.map((_,i)=>i).filter(i=>!shown(i)).map(i=>'.c'+i).join(',')+'{display:none}';
 buildHeader()}
// --- 多段見出し：表示中の列の祖先グループ（D.hpath、学位の内訳を開いたときは学位も）から colspan / rowspan を組む。先頭行ほど上に固定（行の高さは CSS で固定） ---
const HH=38,GINFO={'募集':'配属先と、その応募枠。','日程':'全募集回のうち最も早い日。','企業情報':'配属先の企業情報。配属先の指定がない枠は採用元の値。'};
let startCols=new Set();
// 見出しの操作：クリック＝並べ替え（昇順→降順→解除）。マウスは重ねると説明、タッチは長押しで説明（長押しのあとは並べ替えない）
function sortClick(i){if(sortCol!==i){sortCol=i;sortDir=1}else if(sortDir===1)sortDir=-1;else sortCol=-1;render()}
function headerEvents(th,i){let hov=0,lp=0,fired=false;
 const hoverOk=e=>e.pointerType==='mouse'&&matchMedia('(hover:hover)').matches;
 th.addEventListener('pointerenter',e=>{if(!hoverOk(e))return;clearTimeout(hov);hov=setTimeout(()=>openPop(th,headerPop(i)),250)});
 th.addEventListener('pointerleave',()=>clearTimeout(hov));
 th.addEventListener('pointerdown',e=>{if(e.pointerType==='mouse')return;fired=false;clearTimeout(lp);lp=setTimeout(()=>{fired=true;openPop(th,headerPop(i))},500)});
 ['pointerup','pointercancel','pointerleave'].forEach(t=>th.addEventListener(t,()=>clearTimeout(lp)));
 th.addEventListener('contextmenu',e=>{if(e.pointerType!=='mouse'||fired)e.preventDefault()});
 th.onclick=()=>{if(fired){fired=false;return}clearTimeout(hov);sortClick(i)}}
function buildHeader(){
 const info=D.columns.map((_,i)=>i).filter(shown).map(i=>{const [r,k]=D.fold[i];const path=[...D.hpath[i]];let label=D.headers[i];
  if(r==='deg'){label=D.columns[i].slice(0,2);if(fold.d[k]){path.push(label);label='月給'}}
  else if(r==='isub'){label=D.headers[i]}
  else if(r==='sub'){label=D.columns[i].slice(2);if(label==='固定残業代')label='固残'}
  return {i,path,label,r,k}});
 const H=1+Math.max(1,...info.map(x=>x.path.length)),rows=[...Array(H)].map(()=>el('tr'));
 const top=x=>x.path[0]||D.columns[x.i];startCols=new Set();
 info.forEach((x,j)=>{if(j>1&&top(x)!==top(info[j-1])&&info[j-1].i!==1)startCols.add(x.i)});
 for(let L=0;L<H;L++){let j=0;
  while(j<info.length){const x=info[j];
   if(x.path.length<L){j++;continue}
   if(x.path.length===L){const i=x.i,th=el('th',undefined,'c'+i+(startCols.has(i)?' gs':'')+(D.columns[i]==='採用元'?' pubc':''));th.append(el('span',x.label,'lb'));th.dataset.col=i;th.title=D.full[i];th.rowSpan=H-L;th.style.top=L*HH+'px';
    if(numeric.has(D.columns[i]))th.classList.add('num');
    if(x.r==='smax')th.append(tg('▸','選考を全10種の列に展開',()=>{fold.s=true}));
    if(x.r==='imax')th.append(tg('▸','業種を1位〜3位の3列に展開',()=>{fold.i=true}));
    if(x.r==='max')th.append(tg('▸','学位別の3列に展開',()=>{fold.e=true}));
    if(x.r==='deg'&&!fold.d[x.k])th.append(tg('▸','内訳の列（基本給・固定残業代・その他）を表示',()=>{fold.d[x.k]=true}));
    headerEvents(th,i);rows[L].append(th);j++;continue}
   const key=x.path.slice(0,L+1).join('/');let n=1;
   while(j+n<info.length&&info[j+n].path.length>L&&info[j+n].path.slice(0,L+1).join('/')===key)n++;
   const name=x.path[L],th=el('th',name,'gh'+(L===0?' g0':'')+(startCols.has(x.i)?' gs':''));th.colSpan=n;th.style.top=L*HH+'px';
   const fo=name==='業種'?()=>{fold.i=false}:name==='選考'?()=>{fold.s=false}:name==='初任給'?()=>{fold.e=false}:DEGREE_NAMES.includes(name)?()=>{fold.d[DEGREE_NAMES.indexOf(name)]=false}:null;
   if(fo){th.append(tg('▾','',fo));th.querySelector('.tg').title=name==='業種'?'業種を主な事業の1列に畳む':name==='初任給'?'初任給を1列に畳む':name==='選考'?'選考をまれな選考の1列に畳む':name+'の内訳の列を隠す';th.title='クリックで畳む';th.onclick=()=>setFold(fo)}
   else{th.title=GINFO[name]||'';th.onclick=()=>openPop(th,p=>{popHead(p,name);p.append(el('p',GINFO[name]||''))})}
   rows[L].append(th);j+=n}}
 hd.replaceChildren(...rows);
 [...hd.querySelectorAll('th[data-col]')].forEach(th=>{const i=+th.dataset.col;th.classList.toggle('sorted',i===sortCol);th.querySelector('.lb').dataset.dir=sortDir>0?'▲':'▼'})}
const SHK='viewerShiftPrev';let shiftOn=false;
try{shiftOn=localStorage.getItem(SHK)==='1'}catch(e){}
const shx=document.getElementById('shift');shx.checked=shiftOn;
const SHT='前年度の日付を1年後ろにずらして表示';
const sv=c=>shiftOn&&c[5]?c[5]:null; // ずらした [表示文字列, ソートキー]（前年度由来の日付のみ）
const txt=c=>(sv(c)||c)[0];
function vnode(c){const s=sv(c);if(!s)return document.createTextNode(c[0]);const e=el('span',s[0],'sh');e.title=SHT;return e}
const text=r=>r.slice(0,NC).map(c=>c[0]+' '+(c[5]?c[5][0]+' ':'')+c[2]+' '+(c[3]||'')).join(' ').toLowerCase();
D.rows.forEach(r=>r.hay=text(r));
function key(c,i){if(c[0]==='—')return null;const s=sv(c)?sv(c)[1]:c[1];
 if(numeric.has(D.columns[i])){const n=typeof s==='number'?s:parseFloat(String(c[0]).replace(/,/g,''));return isNaN(n)?String(c[0]):n}
 if(typeof s==='number')return s;return String(s??c[0])}
function cmp(a,b){if(a===null&&b===null)return 0;if(a===null)return 1;if(b===null)return -1;
 if(typeof a==='number'&&typeof b==='number')return a-b;return String(a).localeCompare(String(b),'ja',{numeric:true})}
// --- 〃（規約は SALARY_KEYS の注釈）。表示だけの処理で、値・並べ替え・絞り込み・根拠ポップアップは元のセルのまま ---
function dittoSrc(r,i){const [ro,k]=D.fold[i];if((ro!=='deg'&&ro!=='sub')||k<1||r[i][0]==='—')return -1;
 const nm=D.columns[i].slice(2),c=r[i];
 for(let j=k-1;j>=0;j--){const jc=CI(DEGREE_NAMES[j]+nm);if(jc<0||!shown(jc)||r[jc][0]==='—')continue;
  const p=r[jc];return p[0]===c[0]&&(p[2]||'')===(c[2]||'')&&(p[3]||'')===(c[3]||'')&&(p[4]||'')===(c[4]||'')?jc:-1}
 return -1}
function render(){
 closePop();applyFold();
 let rows=D.rows.filter(r=>r.hay.includes(query));
 if(sortCol>=0){const k=rows.map(r=>[r,key(r[sortCol],sortCol)]);
  k.sort((x,y)=>{if(x[1]===null||y[1]===null)return cmp(x[1],y[1]);return sortDir*cmp(x[1],y[1])});rows=k.map(x=>x[0])}
 if(!narrow()&&rows.length&&!rows.includes(selected))selected=rows[0];
 const f=document.createDocumentFragment();selTd=null;
 rows.forEach(r=>{const tr=document.createElement('tr');if(r===selected)tr.className='sel';
  tr.onclick=e=>{const td=e.target.closest('td');selected=r;selCol=td?td.cellIndex:-1;render();
   document.getElementById('panel').scrollTop=0;
   if(selTd&&cellDetail(r,selCol))openPop(selTd,cellPop(r,selCol));
   else if(narrow())document.getElementById('panel').scrollIntoView({behavior:'smooth',block:'start'})};
  r.slice(0,NC).forEach((c,i)=>{const td=document.createElement('td');const on=r===selected&&i===selCol;
   td.className='c'+i+(startCols.has(i)?' gs':'')+(D.columns[i]==='採用元'?' pubc':'')+(on?' selc':'')+(numeric.has(D.columns[i])?' num':'')+(c[0]==='—'?' na':'')+((c[2]||'').includes('前')?' pv':'');
   if(on)selTd=td;
   const dj=dittoSrc(r,i);
   if(dj>=0){td.classList.remove('pv');td.classList.add('dit');td.append(el('span','〃','dit'));
    const words=[...(c[2]||'')].map(m=>MK[m][1]).join('・');
    td.title=[D.columns[dj].slice(0,2)+'と同額（'+(c[3]||txt(c)+unitOf(i))+'）',words].filter(Boolean).join('\n');tr.appendChild(td);return}
   if(c[4])td.append(el('span',c[4],'pre'));
   const mk=c[2]||'',vn=vnode(c);
   if(mk){const w=el('span',undefined,['mk',...[...mk].map(m=>MK[m][0])].join(' '));w.append(vn);td.append(w)}else td.append(vn);
   const words=[...mk].map(m=>MK[m][1]).join('・'),base=(i<2||c[3])?(c[3]||txt(c)):'';
   if(base||words)td.title=[base,words].filter(Boolean).join('\n');tr.appendChild(td)});f.appendChild(tr)});
 tb.replaceChildren(f);
 renderPanel();
 info.textContent=`${rows.length} / ${D.rows.length} 行　生成: ${D.built_at}${D.skipped?`　（旧形式等で ${D.skipped} 件を除外）`:''}`;
}
function cellDetail(r,i){const d=r[NC];if(i===1)return d.slot;
 if(D.fold[i][0]==='ssub'){ // 選考の各列は「選考」列の候補・注記から、その選考の分だけ取り出す（別データは持たない）
  const all=(D.groups[d.g]||{})[CI('選考')];if(!all)return null;const l=D.columns[i];
  const c=all[0].filter(x=>x[5]===l),n=all[1].filter(x=>x[0].startsWith(l+' '));return c.length||n.length?[c,n]:null}
 return (D.groups[d.g]||{})[i]||((d.p&&D.companies[d.p])||{})[i]||null}
function safeUrl(u){try{const x=new URL(u);return x.protocol==='http:'||x.protocol==='https:'?x:null}catch(e){return null}}
// --- ポップアップ（セルの根拠・列の説明）。anchor の近くに出し、画面からはみ出さない ---
let popAnchor=null,leaveTimer=0;
function cancelLeave(){clearTimeout(leaveTimer);leaveTimer=0}
function leaveClose(e){if(e.pointerType!=='mouse'||matchMedia('(max-width:800px)').matches)return;
 cancelLeave();leaveTimer=setTimeout(closePop,250)}
function enterKeep(e){if(e.pointerType==='mouse')cancelLeave()}
pop.addEventListener('pointerleave',leaveClose);pop.addEventListener('pointerenter',enterKeep);
function openPop(anchor,build){
 cancelLeave();
 if(popAnchor){popAnchor.removeEventListener('pointerleave',leaveClose);popAnchor.removeEventListener('pointerenter',enterKeep)}
 popAnchor=anchor;anchor.addEventListener('pointerleave',leaveClose);anchor.addEventListener('pointerenter',enterKeep);
 pop.replaceChildren();build(pop);pop.hidden=false;
 if(matchMedia('(max-width:800px)').matches){pop.style.left=pop.style.top='';return}
 const a=anchor.getBoundingClientRect(),w=pop.offsetWidth,h=pop.offsetHeight,vw=innerWidth,vh=innerHeight,g=8;
 let top=a.bottom+4;if(top+h>vh-g)top=a.top-h-4>=g?a.top-h-4:Math.max(g,vh-h-g);
 pop.style.left=Math.max(g,Math.min(a.left,vw-w-g))+'px';pop.style.top=top+'px'}
function closePop(){cancelLeave();pop.hidden=true;pop.replaceChildren()}
function popHead(p,title,marks){const h=el('div',undefined,'ph'),t=el('span',title);badges(t,marks);
 const x=el('button','✕','x');x.type='button';x.title='閉じる';x.setAttribute('aria-label','閉じる');x.onclick=closePop;h.append(t,x);p.append(h)}
function headerPop(i){return p=>{popHead(p,D.full[i]);
 p.append(el('p',D.desc[i].short));
 if(D.desc[i].definition.length){const d=el('details',undefined,'def');d.append(el('summary','調査での定義'));
  D.desc[i].definition.forEach(t=>d.append(el('p',t)));p.append(d)}
 }}
function slotPop(r,p){const s=r[NC].slot,row=(k,v)=>{const d=el('div');d.append(el('span',k,'k'),' ',v);p.append(d)};
 popHead(p,D.full[1]+'：'+s.name,r[1][2]);
 row('応募年度',s.year+(s.prev?'（前年度の枠のみ。'+s.ry+'年度の同名の枠がなく、前年度の枠の値を表示 = 「前」）':''));
 row('配属先',s.assign||'未指定（採用元の情報を表示）');
 if(s.others.length)row('同じ枠の他の配属先',s.others.join('、'));
 s.src.forEach(x=>{const u=x[1]&&safeUrl(x[1]),d=el('div');d.append(el('span','枠の出典 '+x[0],'k'),' ');
  if(u){const a=el('a',u.hostname);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';a.title=u.href;d.append(a)}else d.append(x[1]||'—');p.append(d)});
 if(s.note)row('注記',s.note);
 const rw=(D.groups[r[NC].g]||{}).remote;
 if(rw){p.append(el('div','リモート勤務','k'));cands(p,rw)}else row('リモート勤務','情報なし')}
function cellPop(r,i){return p=>{if(i===1)return slotPop(r,p);const cd=cellDetail(r,i);
 popHead(p,D.full[i]+'：'+(r[i][4]?r[i][4]+' ':'')+txt(r[i]),r[i][2]);cands(p,cd)}}
function cands(p,cd){
 const YL=['','今年度','前年度'],KL={'公':'公式','非':'非公式','?':'不明'};
 if(cd[0].length){const ul=el('ul');
  cd[0].forEach(c=>{const sh=shiftOn&&c[1]===2&&c[6],li=el('li',sh?undefined:c[0]);
   if(sh){const e=el('span',c[6],'sh');e.title=SHT;li.append(e)}
   if(c[4])li.append(el('span','採用','ok'));
   li.append(el('span',[YL[c[1]],KL[c[2]]||c[2],c[5]].filter(Boolean).join('・'),'k'));
   const raw=c[3]<0?'':D.urls[c[3]];const u=raw&&safeUrl(raw);
   if(u){li.append(' ');const a=el('a',u.hostname);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';a.title=u.href;li.append(a)}
   else if(raw)li.append(el('span',raw,'k'));
   ul.append(li)});p.append(ul)}
 cd[1].forEach(n=>{const d=el('div');d.style.marginTop='6px';d.append(el('div',n[0],'k'),el('div',D.notes[n[1]],'note'));p.append(d)})}
document.addEventListener('mousedown',e=>{if(!pop.hidden&&!pop.contains(e.target))closePop()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePop()});
addEventListener('resize',closePop);
const CI=n=>D.columns.indexOf(n),unitOf=i=>{const m=/（(.+)）$/.exec(D.full[i]);return m?m[1]:''};
function valNode(c){const v=el('span');if(c[0]==='—'){v.className='na';v.append('—');return v}
 if(c[4])v.append(el('span',c[4],'pre'),' ');v.append(vnode(c));badges(v,c[2]);return v}
function renderPanel(){
 const p=document.getElementById('panel');p.replaceChildren();
 if(!selected){p.append(el('div','行をクリックすると、募集回・選考工程・企業情報を表示します','empty'));return}
 const d=selected[NC],r=selected;
 p.append(el('h2',d.title[0]));if(d.title[1])p.append(el('div',d.title.slice(1).join(' / '),'psub'));
 if(d.pub)p.append(el('div','採用元: '+d.pub,'psub'));
 const stats=el('div',undefined,'stats');
 [['初任給',1],['平均年収'],['月残業'],['年間休日'],['有給取得'],['最初の締切']].forEach(([n,big])=>{const i=CI(n);if(i<0)return;
  const c=r[i],st=el('div',undefined,'stat'+(big?' big':'')),v=el('div',undefined,'v');
  st.append(el('div',n==='初任給'?'初任給（学位の最大値）':n+(unitOf(i)?'（'+unitOf(i)+'）':''),'l'));
  v.append(valNode(c));if(big&&c[0]!=='—')v.append(el('span','万円/月','u'));st.append(v);stats.append(st)});
 p.append(stats);
 const facts=(names,title)=>{const ul=el('ul',undefined,'facts');
  names.forEach(n=>{const i=CI(n);if(i<0)return;const li=el('li'),v=valNode(r[i]);v.classList.add('v');
   li.append(el('span',n+(unitOf(i)?'（'+unitOf(i)+'）':''),'l'),v);
   if(cellDetail(r,i)){li.classList.add('hasd');li.title='クリックで値の根拠を表示';li.onclick=()=>openPop(li,cellPop(r,i))}
   ul.append(li)});
  p.append(el('h3',title),ul)};
 facts(['学士初任給','修士初任給','博士初任給'],'初任給（学位別）');
 const h=el('h3','選考工程');badges(h,d.process&&d.process.marks);p.append(h);
 if(!d.process)p.append(el('div','—','na'));
 else{const ol=el('ol',undefined,'steps');d.process.stages.forEach(st=>{const li=el('li'),sp=el('span');
  st.forEach((x,i)=>{if(i)sp.append(' ／ ');sp.append(x)});li.append(sp);ol.append(li)});p.append(ol)}
 const h2=el('h3','募集回');badges(h2,d.wmarks);p.append(h2);
 if(!d.windows.length)p.append(el('div','—','na'));
 d.windows.forEach(w=>{const div=el('div',undefined,'win'),dd=el('div',undefined,'d');div.append(el('div',w.name,'n'));
  const on=shiftOn&&w.open1!==undefined;
  [['開始',on?w.open1:w.open],['締切',on?w.deadline1:w.deadline]].forEach(([l,t])=>{const s=el('span',l),e=!t||t==='—';const dt=el('span',e?'未定':t,'dt'+(e?' e':'')+(on&&!e?' sh':''));if(on&&!e)dt.title=SHT;s.append(dt);dd.append(s)});
  div.append(dd);p.append(div)});
 const skip=new Set([...D.columns.filter((_,i)=>D.fold[i][0]==='ssub'||D.fold[i][0]==='isub'),'配属先','採用元','応募枠','最初の募集開始','最初の締切','選考','初任給','平均年収','月残業','年間休日','有給取得']);
 facts(D.columns.filter(n=>!skip.has(n)&&!/^(学士|修士|博士)/.test(n)),'企業情報（配属先）');
}
shx.onchange=()=>{shiftOn=shx.checked;try{localStorage.setItem(SHK,shiftOn?'1':'0')}catch(e){}render()};
document.getElementById('q').oninput=e=>{query=e.target.value.trim().toLowerCase();render()};
render();
}
__BOOT__
</script>
</body>
</html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
