import base64
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_viewer as bv  # noqa: E402


def dr(v):
    """Exact-day DateRange from an ISO string."""
    return {"earliest": v, "latest": v} if isinstance(v, str) else v


def rec(v, kind="official"):
    return {"value": v, "source": {"kind": kind, "url": "u"}}


def window(kind, *deadlines, opens=()):
    w = {"source": {"kind": kind, "url": "u"},
         "application_deadline_date": {"records": [rec(dr(d), kind) for d in deadlines]}}
    if opens:
        w["application_open_date"] = {"records": [rec(dr(d), kind) for d in opens]}
    return w


def slot(year, ids, salary=None, windows=(), name="S"):
    return {"slot_name": name, "application_year": year,
            "assignment_companies": {"company_ids": ids},
            "application_windows": {"records": list(windows)},
            "starting_salary": {"records": [
                rec({"monthly_total_yen": v}, k) for v, k in (salary or [])]}}


def profile(cid, name):
    return {"company_id": cid, "legal_name": name, "basic_information": {
        "employee_count": {"records": [rec(100.0)]}}}


PROFILES = {"P": profile("P", "採用元社"), "A": profile("A", "配属A"), "B": profile("B", "配属B")}
COL = {c: i for i, c in enumerate(bv.COLUMNS)}


class BuildRowsTest(unittest.TestCase):
    def rows(self, slots):
        return self.built(slots)[0]

    def built(self, slots):
        return bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                         "application_slots": slots}])

    def test_priority_and_marks(self):
        r = self.rows([slot(2028, ["A"], [(300000, "unofficial")]),
                       slot(2027, ["A"], [(250000, "official")])])[0]
        self.assertEqual(r[COL["学士初任給"]], ["25.0", 250000, "前"])
        r = self.rows([slot(2028, ["A"], [(300000, "unofficial"), (310000, "unofficial")])])[0]
        self.assertEqual(r[COL["学士初任給"]][2], "非+")
        self.assertEqual(r[COL["修士初任給"]][0], "—")

    def salary_row(self, **degrees):
        sl = slot(2028, ["A"])
        for key, v in degrees.items():
            sl[key] = {"records": [rec(v)]}
        return self.rows([sl])[0]

    def test_max_degree_column(self):
        r = self.salary_row(starting_salary={"monthly_total_yen": 240000},
                            starting_salary_master={"monthly_total_yen": 280000},
                            starting_salary_doctor={"monthly_total_yen": 260000})
        self.assertEqual(r[COL["初任給"]], ["28.0", 280000, "", "", "修"])
        self.assertEqual(r[COL["修士初任給"]][0], "28.0")

    def test_max_degree_tie_prefers_lowest_degree(self):
        r = self.salary_row(starting_salary_master={"monthly_total_yen": 280000},
                            starting_salary_doctor={"monthly_total_yen": 280000})
        self.assertEqual(r[COL["初任給"]][4], "修")
        r = self.salary_row(starting_salary={"monthly_total_yen": 280000},
                            starting_salary_doctor={"monthly_total_yen": 280000})
        self.assertEqual(r[COL["初任給"]][4], "学")
        self.assertEqual(self.rows([slot(2028, ["A"])])[0][COL["初任給"]], ["—", None, ""])

    def test_max_degree_uses_chosen_degree_marks_and_detail(self):
        sl = slot(2028, ["A"], [(240000, "official")])
        sl["starting_salary_master"] = {"records": [rec({"monthly_total_yen": 300000}, "unofficial")]}
        rows, groups, _ = self.built([sl])
        self.assertEqual(rows[0][COL["初任給"]][2], "非")
        g = groups[rows[0][-1]["g"]]
        self.assertEqual(g[str(COL["初任給"])], g[str(COL["修士初任給"])])

    def test_good_direction_columns(self):
        self.assertLessEqual(set(bv.GOOD_DIRECTION), set(bv.COLUMNS))
        for c in ("初任給", "学士基本給", "博士その他", "平均年収", "リモート制度", "えるぼし", "研究開発費"):
            self.assertEqual(bv.GOOD_DIRECTION[c], 1, c)
        for c in ("月残業", "3年離職率", "学士固定残業代", "修士固定残業代", "博士固定残業代"):
            self.assertEqual(bv.GOOD_DIRECTION[c], -1, c)

    def test_salary_breakdown_columns(self):
        r = self.salary_row(starting_salary={"monthly_total_yen": 245000, "base_salary_yen": 200000,
                                             "fixed_overtime_yen": 41000, "fixed_overtime_hours": 20})
        self.assertEqual(r[COL["学士基本給"]][:2], ["20.0", 200000])
        self.assertEqual(r[COL["学士固定残業代"]], ["4.1", 41000, "", "4.1万円/20時間"])
        self.assertEqual(r[COL["学士その他"]][:2], ["0.4", 4000])
        # 固定残業代の記載なしは 0 円として扱う
        r = self.salary_row(starting_salary={"monthly_total_yen": 245000, "base_salary_yen": 200000})
        self.assertEqual(r[COL["学士固定残業代"]][0], "—")
        self.assertEqual(r[COL["学士その他"]][:2], ["4.5", 45000])
        # 基本給または月給額がなければ計算しない（月給額のみ／基本給のみ）
        r = self.salary_row(starting_salary={"monthly_total_yen": 245000})
        self.assertEqual((r[COL["学士初任給"]][0], r[COL["学士基本給"]][0], r[COL["学士その他"]][0]), ("24.5", "—", "—"))
        r = self.salary_row(starting_salary={"base_salary_yen": 200000})
        self.assertEqual((r[COL["学士初任給"]][0], r[COL["学士基本給"]][0], r[COL["学士その他"]][0]), ("20.0", "20.0", "—"))

    def test_breakdown_comes_from_top_record(self):
        sl = slot(2028, ["A"])
        sl["starting_salary"] = {"records": [
            rec({"monthly_total_yen": 250000, "base_salary_yen": 200000}, "official"),
            rec({"monthly_total_yen": 300000, "base_salary_yen": 280000}, "unofficial")]}
        r = self.rows([sl])[0]
        self.assertEqual((r[COL["学士初任給"]][0], r[COL["学士基本給"]][0], r[COL["学士その他"]][0]), ("25.0", "20.0", "5.0"))

    def test_unknown_kind_ranks_after_unofficial(self):
        r = self.rows([slot(2028, ["A"], [(300000, "unknown")])])[0]
        self.assertEqual(r[COL["学士初任給"]], ["30.0", 300000, "非"])
        r = self.rows([slot(2028, ["A"], [(300000, "unknown")]),
                       slot(2027, ["A"], [(250000, "unofficial")])])[0]
        self.assertEqual(r[COL["学士初任給"]], ["25.0", 250000, "前非"])
        self.assertEqual(bv.cand("x", 1, rec(1, "unknown"))[2], "?")

    def test_previous_only_year_and_assignment(self):
        r = self.rows([slot(2027, ["A"], [(250000, "unofficial")])])[0]
        self.assertEqual(r[COL["学士初任給"]][2], "前非")
        self.assertEqual(r[COL["応募枠"]], ["S", "S", "前"])
        self.assertNotIn("年度", COL)
        self.assertEqual(r[-1]["slot"]["assign"], "配属A")
        self.assertTrue(r[-1]["slot"]["prev"])
        self.assertEqual(r[-1]["slot"]["year"], 2027)

    def test_empty_assignment_uses_publisher(self):
        r = self.rows([slot(2028, [])])[0]
        self.assertIsNone(r[-1]["slot"]["assign"])
        self.assertEqual(r[COL["配属先"]][0], "採用元社")
        self.assertEqual(r[COL["採用元"]][0], "採用元社")
        self.assertNotIn("pub", r[-1])
        self.assertEqual(r[COL["従業員数"]][0], "100")

    def test_multiple_assignments_and_missing_profile(self):
        rows = self.rows([slot(2028, ["A", "B", "Z"])])
        self.assertEqual([r[-1]["slot"]["assign"] for r in rows], ["配属A", "配属B", "Z"])
        self.assertEqual(rows[0][-1]["slot"]["others"], ["配属B", "Z"])
        self.assertEqual(rows[2][COL["従業員数"]][0], "—")

    def test_deadlines(self):
        r = self.rows([slot(2028, ["A"], windows=[window("official", "2028-03-01", "2028-01-10", opens=("2027-12-01", "2027-11-05")),
                                                   window("unofficial", "2027-01-01", opens=("2026-01-01",))])])[0]
        self.assertEqual(r[COL["最初の締切"]][0], "28/1/10")
        self.assertEqual(r[COL["最初の締切"]][1], "2028-01-10")
        self.assertEqual(r[COL["最初の募集開始"]][0], "27/11/5")

    def test_shift_range_and_leap_day(self):
        self.assertEqual(bv.shifted(dr("2027-05-05")), ["28/5/5", "2028-05-05"])
        self.assertEqual(bv.shift_range(dr("2024-02-29")), dr("2025-02-28"))
        self.assertEqual(bv.shifted({"earliest": "2027-02-01", "latest": "2027-12-31"})[0], "28/2/1–12/31")
        self.assertEqual(bv.shifted({"earliest": "2027-12-20", "latest": "2028-01-10"})[0], "28/12/20–29/1/10")

    def test_previous_year_dates_carry_shifted_variant(self):
        cur = slot(2028, ["A"], windows=[window("official", "2028-02-29")])
        prev = slot(2027, ["A"], windows=[window("official", "2027-05-05", opens=("2027-02-28",))])
        # 前年度のみ: 前の印が付く日付には +1年版（表示文字列, ソートキー）が付く
        r = self.rows([prev])[0]
        self.assertEqual(r[COL["最初の締切"]], ["27/5/5", "2027-05-05", "前", "", "", ["28/5/5", "2028-05-05"]])
        self.assertEqual(r[-1]["windows"][0]["deadline1"], "28/5/5")
        self.assertEqual(r[-1]["windows"][0]["open1"], "28/2/28")
        cands, _ = self.cell_detail([prev], "最初の締切")
        self.assertEqual(cands[0][1], 2)
        self.assertEqual(cands[0][6], "28/5/5")
        # 今年度の日付は変えない（ずらし版を持たない）
        r = self.rows([cur])[0]
        self.assertEqual(r[COL["最初の締切"]], ["28/2/29", "2028-02-29", ""])
        self.assertNotIn("deadline1", r[-1]["windows"][0])
        cands, _ = self.cell_detail([cur], "最初の締切")
        self.assertEqual(len(cands[0]), 6)
        # 今年度が優先されるときは今年度の値のまま、前年度の候補だけがずらし版を持つ
        r = self.rows([cur, prev])[0]
        self.assertEqual(len(r[COL["最初の締切"]]), 3)
        cands, _ = self.cell_detail([cur, prev], "最初の締切")
        self.assertEqual([(c[1], len(c)) for c in cands], [(1, 6), (2, 7)])

    def sel_slot(self, year, **flags):
        s = slot(year, ["A"])
        s["selection_process"] = {f: {"records": [rec(v, k)]} for f, (v, k) in flags.items()}
        return s

    def test_selection_folded_and_expanded_columns(self):
        r = self.rows([self.sel_slot(2028, has_interview=(True, "official"), has_web_test=(True, "official"),
                                     has_coding_task=(True, "official"), has_group_discussion=(True, "official"),
                                     has_case_interview=(False, "official"))])[0]
        # まれな選考だけを固定順（G 集 ケ 課 研 コ）で。ケースは「なし」、面接・Web は出さない
        self.assertEqual(r[COL["選考"]], ["G コ", 2, "", "GD・コーディング"])
        self.assertEqual([r[COL[l]][0] for l in bv.SELECTION_LABELS],
                         ["—", "○", "—", "—", "○", "—", "○", "×", "—", "○"])
        self.assertEqual(r[COL["ケース"]], ["×", 1, ""])
        self.assertEqual(r[COL["GD"]][1], 2)
        # 該当なしは空のセル（「なし」や — ではない）
        r = self.rows([self.sel_slot(2028, has_interview=(True, "official"))])[0]
        self.assertEqual(r[COL["選考"]][:2], ["", 0])
        r = self.rows([slot(2028, ["A"])])[0]
        self.assertEqual(r[COL["選考"]][:2], ["", 0])
        self.assertEqual(r[COL["ES"]][0], "—")

    def test_selection_columns_carry_badges(self):
        r = self.rows([self.sel_slot(2028, has_group_interview=(True, "unofficial")),
                       self.sel_slot(2027, has_group_interview=(True, "official"), has_case_interview=(True, "unofficial"))])[0]
        self.assertEqual(r[COL["集団面接"]][:3], ["○", 2, "前"])  # 前年度公式が今年度非公式に優先
        self.assertEqual(r[COL["ケース"]][2], "前非")
        self.assertEqual(r[COL["選考"]][0], "集 ケ")
        self.assertEqual(r[COL["選考"]][2], "前非")

    def test_header_group_metadata(self):
        path = {c: bv.header_path(c) for c in bv.COLUMNS}
        self.assertEqual(path["応募枠"], ["募集"])
        self.assertEqual(path["最初の締切"], ["日程"])
        self.assertEqual(path["選考"], [])
        self.assertEqual(path["ES"], ["選考"])
        self.assertEqual(path["初任給"], [])
        self.assertEqual(path["学士初任給"], ["初任給"])
        self.assertEqual(path["修士固定残業代"], ["初任給", "修士"])
        self.assertEqual(path["設立"], ["企業情報"])
        html = bv.build_html(*self.built([slot(2028, ["A"])]))
        self.assertIn("function buildHeader", html)
        self.assertIn('id="hd"', html)

    def test_header_click_sorts_and_popup_has_no_sort_buttons(self):
        html = bv.build_html(*self.built([slot(2028, ["A"])]))
        self.assertIn("function sortClick", html)
        self.assertIn("if(sortCol!==i){sortCol=i;sortDir=1}else if(sortDir===1)sortDir=-1;else sortCol=-1", html)
        self.assertIn("setTimeout(()=>openPop(th,headerPop(i)),250)", html)
        self.assertIn("},500)", html)
        self.assertNotIn("良い順", html)
        # 折りたたみの ▸/▾ は stopPropagation で見出しのクリック（並べ替え）に伝えない
        self.assertIn("t.onclick=e=>{e.stopPropagation();setFold(f)}", html)

    def test_selection_flag_candidates_available_for_columns(self):
        cands, _ = self.cell_detail([self.sel_slot(2028, has_web_test=(True, "official"))], "選考")
        self.assertEqual([c[5] for c in cands], ["Web"])

    def test_unknown_kind_window_deadline(self):
        s = slot(2028, ["A"], windows=[window("unknown", "2028-02-02")])
        r = self.rows([s])[0]
        self.assertEqual(r[COL["最初の締切"]], ["28/2/2", "2028-02-02", "非"])
        d = r[-1]
        self.assertEqual((d["wmarks"], d["windows"][0]["deadline"]), ("非", "28/2/2"))
        cands, _ = self.cell_detail([s], "最初の締切")
        self.assertEqual(cands[0][2:5], ["?", "u", 1])
        s["application_windows"]["records"].append(window("unofficial", "2028-09-09"))
        self.assertEqual(self.rows([s])[0][COL["最初の締切"]][1], "2028-09-09")

    def test_detail_priority(self):
        a = slot(2028, ["A"], windows=[window("unofficial", "2028-01-01")])
        b = slot(2027, ["A"], windows=[window("official", "2027-05-05")])
        a["selection_process"] = {"process": {"records": [rec({"stages": [{"steps": [
            {"kind": "interview", "label": "面接"}]}]}, "unofficial")]}}
        b["selection_process"] = {"process": {"records": [rec({"stages": []})]}}
        d = self.rows([a, b])[0][-1]
        self.assertEqual(d["title"], ["配属A", "S"])
        self.assertEqual(d["pub"], "採用元社")
        self.assertEqual((d["process"]["marks"], d["process"]["stages"]), ("前", []))
        self.assertEqual((d["wmarks"], d["windows"][0]["deadline"]), ("前", "27/5/5"))
        a["selection_process"]["process"]["records"][0]["source"]["kind"] = "official"
        self.assertEqual(self.rows([a, b])[0][-1]["process"]["stages"][0][0], "面接")

    def cell_detail(self, slots, col, who="g"):
        rows, groups, companies = self.built(slots)
        d = rows[0][-1]
        store = groups[d["g"]] if who == "g" else companies[d["p"]]
        return store[str(COL[col])]

    def test_salary_candidates_in_priority_order(self):
        a = slot(2028, ["A"], [(300000, "unofficial"), (310000, "official")])
        b = slot(2027, ["A"], [(250000, "official")])
        a["starting_salary"]["note"] = "今年の注記"
        b["starting_salary"]["note"] = "前年の注記"
        cands, notes = self.cell_detail([a, b], "学士初任給")
        self.assertEqual([(c[0], c[1], c[2], c[4]) for c in cands],
                         [("31.0万円", 1, "公", 1), ("25.0万円", 2, "公", 0),
                          ("30.0万円", 1, "非", 0)])
        self.assertEqual(cands[0][3], "u")
        self.assertEqual(notes, [["今年度", "今年の注記"], ["前年度", "前年の注記"]])

    def test_previous_only_slot_is_labelled_previous_year(self):
        cands, _ = self.cell_detail([slot(2027, ["A"], [(250000, "unofficial")])], "学士初任給")
        self.assertEqual((cands[0][1], cands[0][2], cands[0][4]), (2, "非", 1))

    def test_empty_cell_keeps_note_only(self):
        a = slot(2028, ["A"])
        a["starting_salary_master"] = {"records": [], "note": "確認できず"}
        cands, notes = self.cell_detail([a], "修士初任給")
        self.assertEqual((cands, notes), ([], [["今年度", "確認できず"]]))
        self.assertNotIn(str(COL["博士初任給"]), self.built([a])[1][0])

    def test_deadline_candidates_list_every_window(self):
        w1, w2 = window("official", "2028-03-01", "2028-01-10"), window("unofficial", "2027-01-01")
        w1["window_name"] = "第1回"
        w1["application_deadline_date"]["note"] = "締切注記"
        s = slot(2028, ["A"], windows=[w1, w2])
        cands, notes = self.cell_detail([s], "最初の締切")
        self.assertEqual([(c[0], c[4], c[5]) for c in cands],
                         [("28/3/1", 0, "第1回"), ("28/1/10", 1, "第1回"),
                          ("27/1/1", 0, "—")])
        self.assertEqual(notes, [["第1回 今年度", "締切注記"]])
        w1["application_open_date"] = {"records": [rec(dr("2027-12-01")), rec(dr("2027-11-05"))]}
        cands, _ = self.cell_detail([s], "最初の募集開始")
        self.assertEqual([(c[0], c[4]) for c in cands], [("27/12/1", 0), ("27/11/5", 1)])

    def test_selection_candidates_per_flag(self):
        a = slot(2028, ["A"])
        a["selection_process"] = {"has_interview": {"records": [rec(True, "unofficial")], "note": "体験談"}}
        b = slot(2027, ["A"])
        b["selection_process"] = {"has_interview": {"records": [rec(False)]}}
        cands, notes = self.cell_detail([a, b], "選考")
        self.assertEqual([(c[0], c[1], c[4], c[5]) for c in cands],
                         [("×", 2, 1, "面接"), ("○", 1, 0, "面接")])
        self.assertEqual(notes, [["面接 今年度", "体験談"]])

    def test_profile_details_are_per_company_not_per_row(self):
        p = profile("A", "配属A")
        p["basic_information"]["employee_count"] = {
            "records": [rec(100.0), rec(200.0, "unofficial")],
            "note": "連結と単体"}
        rows, groups, companies = bv.build_rows({**PROFILES, "A": p}, [{
            "recruitment_year": 2028, "publishing_company_id": "P",
            "application_slots": [slot(2028, ["A"], name="X"), slot(2028, ["A"], name="Y")]}])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][-1]["p"], "A")
        cands, notes = companies["A"][str(COL["従業員数"])]
        self.assertEqual([(c[0], c[1], c[2], c[4]) for c in cands],
                         [("100", 0, "公", 1), ("200", 0, "非", 0)])
        self.assertEqual(notes, [["企業情報", "連結と単体"]])

    def test_names_are_shortened_with_full_name_kept(self):
        rows = bv.build_rows({"P": profile("P", "株式会社採用元"), "A": profile("A", "配属A(株)")}, [{
            "recruitment_year": 2028, "publishing_company_id": "P",
            "application_slots": [slot(2028, ["P", "A"])]}])[0]
        self.assertEqual([r[COL["採用元"]] for r in rows], [["採用元", "採用元", "", "株式会社採用元"]] * 2)
        self.assertEqual(rows[0][COL["配属先"]], ["採用元", "採用元", "", "株式会社採用元"])
        self.assertEqual(rows[1][COL["配属先"]][3], "配属A(株)")
        self.assertEqual([r[-1]["slot"]["assign"] for r in rows], ["株式会社採用元", "配属A(株)"])
        self.assertEqual([r[-1]["title"][0] for r in rows], ["株式会社採用元", "配属A(株)"])
        self.assertEqual(COL["採用元"], len(bv.COLUMNS) - 1)
        self.assertEqual(bv.header_path("採用元"), [])
        self.assertEqual(bv.header_path("配属先"), ["募集"])


class OrdinalAndSlotTest(unittest.TestCase):
    def test_ordinal_sort_keys(self):
        k = bv.ordinal_key("eruboshi_certification_tier")
        self.assertEqual([k(v) for v in ("stage_1", "stage_2", "stage_3", "platinum")], [0, 1, 2, 3])
        k = bv.ordinal_key("remote_work_policy")
        self.assertEqual([k(v) for v in ("none", "limited", "hybrid", "full")], [0, 1, 2, 3])
        p = {"company_id": "A", "legal_name": "A", "basic_information": {
            "remote_work_policy": {"records": [rec("hybrid")]},
            "eruboshi_certification_tier": {"records": [rec("platinum")]}}}
        c = bv.profile_cells(p)
        by = dict(zip([x[0] for x in bv.PROFILE_COLUMNS[:1]] + [None] * 3 + [x[0] for x in bv.PROFILE_COLUMNS[1:]], c))
        self.assertEqual(by["remote_work_policy"][:2], ["併用", 2])
        self.assertEqual(by["eruboshi_certification_tier"][1], 3)

    def test_slot_popup_data(self):
        case = {"recruitment_year": 2028, "publishing_company_id": "P",
                "application_slot_inventory": {"records": [
                    {"application_year": 2028, "slot_name": "S", "source": {"kind": "official", "url": "http://x"}},
                    {"application_year": 2028, "slot_name": "T", "source": {"kind": "official", "url": "http://y"}}],
                    "note": "注"},
                "application_slots": [slot(2028, ["A", "B"])]}
        rows = bv.build_rows(PROFILES, [case])[0]
        s = rows[0][-1]["slot"]
        self.assertEqual((s["name"], s["year"], s["prev"], s["assign"], s["others"]),
                         ("S", 2028, False, "配属A", ["配属B"]))
        self.assertEqual(s["src"], [[2028, "http://x"]])
        self.assertEqual(s["note"], "注")
        self.assertEqual(rows[1][-1]["slot"]["assign"], "配属B")


class FormatTest(unittest.TestCase):
    def test_salary_falls_back_to_base(self):
        m = {"records": [{"value": {"monthly_total_yen": None, "base_salary_yen": 408500}},
                         {"value": {"monthly_total_yen": 250000, "base_salary_yen": 230000}}]}
        self.assertEqual([r["value"] for r in bv.salary_metric({"starting_salary": m}, "starting_salary")["records"]],
                         [408500, 250000])

    def test_salary_text_shows_breakdown(self):
        self.assertEqual(bv.salary_text({"monthly_total_yen": 307000, "base_salary_yen": 250000,
                                         "fixed_overtime_yen": 57000, "fixed_overtime_hours": 20}),
                         "30.7万円（基本給 25.0万円・固定残業代 5.7万円/20時間）")
        self.assertEqual(bv.salary_text({"monthly_total_yen": 250000}), "25.0万円")
        self.assertEqual(bv.salary_text({"monthly_total_yen": None, "base_salary_yen": 408500}), "基本給 40.9万円")

    def test_short_name(self):
        for full, short in [("株式会社ソニー", "ソニー"), ("ソニー株式会社", "ソニー"),
                            ("（株）ABC", "ABC"), ("ABC(株)", "ABC"), ("有限会社 X", "X"),
                            ("合同会社X", "X"), ("株式会社", "株式会社"), ("株式会社日立製作所", "日立製作所"),
                            ("東京株式会社支店", "東京株式会社支店")]:
            self.assertEqual(bv.short_name(full), short)

    def test_date_and_money(self):
        self.assertEqual(bv.fmt_date("2027-03-31"), "27/3/31")
        self.assertEqual(bv.fmt_date("随時"), "随時")
        for v, t in [(2256770000000, "22,567.7"), (290300000000, "2,903"), (5.5e8, "5.5"),
                     (1e7, "0.1"), (500, "<0.1"), (0, "0")]:
            self.assertEqual(bv.fmt_money(v), t)

    def test_enum_labels_fall_back_to_raw_value(self):
        f = {c[0]: c[2] for c in bv.PROFILE_COLUMNS}
        self.assertEqual(f["remote_work_policy"]("limited"), "一部")
        self.assertEqual(f["eruboshi_certification_tier"]("platinum"), "プラチナ")
        self.assertEqual(f["eruboshi_certification_tier"]("stage_2"), "2段階")
        self.assertEqual(f["industry_classification_codes"](
            ["L_scientific_research_professional_technical_services", "E_manufacturing"]), "専門サービス/製造")
        self.assertEqual(f["remote_work_policy"]("new_value"), "new_value")

    def test_profile_cell_applies_enum_labels_and_ignores_type_mismatch(self):
        p = profile("P", "採用元社")
        p["basic_information"]["remote_work_policy"] = {"records": [rec("hybrid")]}
        p["basic_information"]["employee_count"] = {"records": [rec("多数")]}
        cells = dict(zip(bv.COLUMNS[bv.COL_INDEX["業種"]:], bv.profile_cells(p)))
        self.assertEqual(cells["リモート制度"][0], "併用")
        self.assertEqual(cells["従業員数"][0], "多数")

    def test_industry_collapsed_is_first_code_and_expanded_has_three_positions(self):
        sci, man = "L_scientific_research_professional_technical_services", "E_manufacturing"
        p = profile("P", "採用元社")
        p["basic_information"]["industry_classification_codes"] = {"records": [rec([sci, man])]}
        cells = dict(zip(bv.COLUMNS[bv.COL_INDEX["業種"]:], bv.profile_cells(p)))
        self.assertEqual(cells["業種"][0], "専門サービス")
        self.assertEqual(cells["業種"][3], "専門サービス（学術研究，専門・技術サービス業）/製造")
        self.assertEqual(cells["業種1位"][3], "専門サービス（学術研究，専門・技術サービス業）")
        self.assertEqual(len(cells["業種2位"]), 3)
        self.assertEqual([cells[c][0] for c in bv.INDUSTRY_POS], ["専門サービス", "製造", "—"])
        self.assertEqual(bv.fold_role("業種")[0], "imax")
        self.assertEqual(bv.fold_role("業種2位")[0], "isub")
        self.assertEqual(bv.header_path("業種2位"), ["企業情報", "業種"])
        self.assertEqual(bv.header_path("業種"), ["企業情報"])
        d = bv.profile_details(p)
        self.assertEqual(d[bv.COL_INDEX["業種3位"]], d[bv.COL_INDEX["業種"]])
        none = dict(zip(bv.COLUMNS[bv.COL_INDEX["業種"]:], bv.profile_cells(profile("Q", "社"))))
        self.assertEqual([none[c][0] for c in ("業種", *bv.INDUSTRY_POS)], ["—"] * 4)

    def test_decimals_are_uniform_per_column(self):
        def row(income, tenure):
            r = [bv.cell(MISSING_) for MISSING_ in ["—"] * len(bv.COLUMNS)]
            r[bv.COL_INDEX["平均勤続"]] = bv.cell(bv._num(tenure, 1, 1), tenure)
            r[bv.COL_INDEX["平均年収"]] = bv.cell(bv._num(income, 1e4, 0), income)
            return r
        rows = [row(5000000, 30.0), row(6000000, 30.7), row(7000000, 18)]
        cands = [["30", 0, "公", -1, True, ""], ["30.7", 0, "公", -1, False, ""]]
        groups = [{str(bv.COL_INDEX["平均勤続"]): [cands, []]}]
        bv.unify_decimals(rows, groups, {})
        t = bv.COL_INDEX["平均勤続"]
        self.assertEqual([r[t][0] for r in rows], ["30.0", "30.7", "18.0"])
        self.assertEqual([r[t][1] for r in rows], [30.0, 30.7, 18])
        self.assertEqual([c[0] for c in cands], ["30.0", "30.7"])
        self.assertEqual([r[bv.COL_INDEX["平均年収"]][0] for r in rows], ["500", "600", "700"])
        self.assertEqual(bv._pad("30.0万円（基本給 25.5万円）/20時間", 1), "30.0万円（基本給 25.5万円）/20時間")
        self.assertEqual(bv._pad("<0.1", 1), "<0.1")

    def test_every_industry_code_has_a_label(self):
        self.assertTrue(all(v in bv.JSIC_NAMES.values() for v in bv.INDUSTRY_LABELS.values()))
        self.assertEqual(len(bv.INDUSTRY_LABELS), 20)

    def test_numbers_only_in_cells_and_units_in_headers(self):
        f = {c[0]: c[2] for c in bv.PROFILE_COLUMNS}
        self.assertEqual(f["average_annual_income_yen"](12000000), "1,200")
        self.assertEqual(f["average_tenure_years"](7.1), "7.1")
        self.assertEqual(f["established_year"](1950), "1950")
        self.assertEqual(bv.UNITS["平均年収"], "万円")
        self.assertEqual(bv.UNITS["資本金"], "億円")
        self.assertTrue(set(bv.SHORT) <= set(bv.COLUMNS))
        self.assertEqual(len(set(bv.SHORT.get(c, c) for c in bv.COLUMNS)), len(bv.COLUMNS))

    def test_descriptions_come_from_schema(self):
        from schemas.company_profile_models import BasicInformation
        d = bv.column_descriptions()
        self.assertEqual(set(d), set(bv.COLUMNS))
        for field, label, _ in bv.PROFILE_COLUMNS:
            self.assertEqual(d[label]["definition"][0], BasicInformation.model_fields[field].description)
        self.assertEqual(d["平均年収"]["definition"][0], "年額の平均年収。")
        self.assertIn("最低額", d["学士初任給"]["definition"][0])
        self.assertIn("最も早い", d["最初の締切"]["short"])

    def test_short_descriptions_are_short(self):
        d = bv.column_descriptions()
        for c in bv.COLUMNS:
            self.assertTrue(d[c]["short"])
            self.assertLessEqual(len(d[c]["short"]), 70 if c == "選考" else 60, c)

    def test_selection_descriptions_exist(self):
        d = bv.column_descriptions()
        for label in bv.SELECTION_LABELS:
            self.assertIn("（○あり ×なし —不明）", d[label]["short"])
            self.assertEqual(len(d[label]["definition"]), 1)
        self.assertIn("コ=コーディング", d["選考"]["short"])
        self.assertEqual(len(d["選考"]["definition"]), 10)


class DittoTest(unittest.TestCase):
    """学位別の月給・内訳で同額を 〃 にする表示（JS の dittoSrc）。データは変えず、表示だけ。"""

    def salary_slot(self, **by_key):
        sl = slot(2028, ["A"])
        for key, (v, kind) in by_key.items():
            sl[key] = {"records": [rec({"monthly_total_yen": v, "base_salary_yen": v - 40000}, kind)]}
        return sl

    def _row(self, record):
        sl = slot(2028, ["A"])
        sl["starting_salary"] = {"records": [rec(record, "official")]}
        return bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                         "application_slots": [sl]}])[0][0]

    def salary_row_base_only(self):
        return self._row({"base_salary_yen": 200000})

    def salary_row_total_only(self):
        return self._row({"monthly_total_yen": 200000})

    def run_ditto(self, sl, shown_cols=None):
        rows, groups, companies = bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                                           "application_slots": [sl]}])
        html = bv.build_html(rows, groups, companies)
        fn = re.search(r"function dittoSrc\(r,i\)\{.*?\n return -1\}", html, re.S).group(0)
        cols = bv.COLUMNS
        script = ("const D=%s;const DEGREE_NAMES=['学士','修士','博士'];const CI=n=>D.columns.indexOf(n);"
                  "const SH=%s;const shown=i=>SH===null||SH.includes(D.columns[i]);%s;"
                  "const r=D.rows[0];console.log(JSON.stringify(D.columns.map((_,i)=>dittoSrc(r,i))))"
                  % (json.dumps({"columns": cols, "fold": [bv.fold_role(c) for c in cols], "rows": rows}, ensure_ascii=False),
                     json.dumps(shown_cols, ensure_ascii=False), fn))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
        return dict(zip(cols, json.loads(out)))

    def test_same_amount_repeats_become_ditto(self):
        sl = self.salary_slot(starting_salary=(307000, "official"), starting_salary_master=(307000, "official"),
                              starting_salary_doctor=(307000, "official"))
        d = self.run_ditto(sl)
        COLS = bv.COLUMNS
        self.assertEqual(d["学士初任給"], -1)
        self.assertEqual(d["修士初任給"], COLS.index("学士初任給"))
        self.assertEqual(d["博士初任給"], COLS.index("修士初任給"))
        self.assertEqual(d["修士固定残業代"], -1)  # 固定残業代の記載なしは「—」
        for part in ("基本給", "その他"):
            self.assertEqual(d["修士" + part], COLS.index("学士" + part))
        self.assertEqual(d["初任給"], -1)  # 畳んだ最大値の列は対象外

    def test_different_amount_or_marks_are_not_ditto(self):
        sl = self.salary_slot(starting_salary=(307000, "official"), starting_salary_master=(330000, "official"),
                              starting_salary_doctor=(330000, "unofficial"))
        d = self.run_ditto(sl)
        self.assertEqual((d["修士初任給"], d["博士初任給"]), (-1, -1))  # 額が違う／印（非）が違う

    def test_compares_with_nearest_degree_that_has_a_value(self):
        sl = self.salary_slot(starting_salary_master=(307000, "official"), starting_salary_doctor=(307000, "official"))
        d = self.run_ditto(sl)
        self.assertEqual(d["博士初任給"], bv.COLUMNS.index("修士初任給"))
        self.assertEqual(d["修士初任給"], -1)  # 左に値のある学位がない
        self.assertEqual(d["学士初任給"], -1)  # 「—」は 〃 にならない

    def test_hidden_breakdown_column_is_not_a_source(self):
        sl = self.salary_slot(starting_salary=(307000, "official"), starting_salary_master=(307000, "official"))
        shown = [c for c in bv.COLUMNS if c not in ("学士基本給",)]
        d = self.run_ditto(sl, shown)
        self.assertEqual(d["修士基本給"], -1)
        self.assertEqual(d["修士初任給"], bv.COLUMNS.index("学士初任給"))

    def test_ditto_keeps_real_value_in_data_and_legend(self):
        sl = self.salary_slot(starting_salary=(307000, "official"), starting_salary_master=(307000, "official"))
        rows, groups, companies = bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                                           "application_slots": [sl]}])
        self.assertEqual(rows[0][COL["修士初任給"]][:2], ["30.7", 307000])  # ソート・絞り込み用の値は元のまま
        html = bv.build_html(rows, groups, companies)
        self.assertIn("〃</span> 左の学位と同額", html)
        self.assertIn("と同額（", html)

    def test_base_only_cell_is_marked_and_sorts_by_number(self):
        r = self.salary_row_base_only()
        c = r[COL["学士初任給"]]
        self.assertEqual(c[:2], ["20.0", 200000])
        self.assertEqual(c[3:5], ["月給額の記載がなく基本給のみ", "基"])
        total = self.salary_row_total_only()[COL["学士初任給"]]
        self.assertEqual(len(total), 3)  # 月給額のセルには接頭辞も title もない
        # 畳んだ最大値の列は学位の頭文字のまま、title だけ基本給のみを示す
        m = r[COL["初任給"]]
        self.assertEqual((m[1], m[3], m[4]), (200000, "月給額の記載がなく基本給のみ", "学"))
        self.assertEqual(len(total_row_max := self.salary_row_total_only()[COL["初任給"]]), 5)
        self.assertEqual(total_row_max[3], "")

    def test_base_only_is_not_ditto_with_total_of_same_number(self):
        t = self
        sl = slot(2028, ["A"])
        sl["starting_salary"] = {"records": [rec({"monthly_total_yen": 200000}, "official")]}
        sl["starting_salary_master"] = {"records": [rec({"base_salary_yen": 200000}, "official")]}
        sl["starting_salary_doctor"] = {"records": [rec({"base_salary_yen": 200000}, "official")]}
        d = t.run_ditto(sl)
        self.assertEqual(d["修士初任給"], -1)  # 同額でも 月給額 と 基本給のみ は別物
        self.assertEqual(d["博士初任給"], bv.COLUMNS.index("修士初任給"))  # 基本給のみ同士は 〃

    def test_base_only_legend(self):
        rows, groups, companies = bv.build_rows(PROFILES, [])
        self.assertIn("基</span> 基本給のみ（月給額の記載なし）", bv.build_html(rows, groups, companies))


class EncryptionTest(unittest.TestCase):
    PASSWORD = "testpass"

    def built(self):
        sl = slot(2028, ["A"], [(300000, "official")], name="秘密の応募枠")
        rows, groups, companies = bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                                           "application_slots": [sl]}])
        return rows, groups, companies, bv.build_html(rows, groups, companies, password=self.PASSWORD)

    def payload(self, html):
        m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
        return json.loads(m.group(1))

    def test_toggle_in_html(self):
        _, _, _, html = self.built()
        self.assertIn('id="shift"', html)
        self.assertIn("前年度の日程を+1年", html)

    def test_round_trip(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        rows, _, _, html = self.built()
        e = self.payload(html)
        self.assertEqual((e["alg"], e["hash"], e["iterations"]), ("AES-256-GCM", "SHA-256", 200_000))
        salt, iv, ct = (base64.b64decode(e[k]) for k in ("salt", "iv", "ct"))
        self.assertEqual((len(salt), len(iv)), (16, 12))
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=e["iterations"]).derive(
            self.PASSWORD.encode())
        data = json.loads(AESGCM(key).decrypt(iv, ct, None).decode("utf-8"))
        self.assertEqual(data["rows"], rows)
        self.assertEqual(data["columns"], bv.COLUMNS)

    def test_wrong_passphrase_fails(self):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        e = self.payload(self.built()[3])
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(e["salt"]),
                         iterations=e["iterations"]).derive(b"wrong")
        with self.assertRaises(InvalidTag):
            AESGCM(key).decrypt(base64.b64decode(e["iv"]), base64.b64decode(e["ct"]), None)

    def test_plaintext_absent_from_html(self):
        _, _, _, html = self.built()
        for text in ("採用元社", "配属A", "秘密の応募枠", "30.0"):
            self.assertNotIn(text, html)
    def test_plaintext_build_has_data_and_no_lock(self):
        rows, groups, companies, _ = self.built()
        html = bv.build_html(rows, groups, companies)
        for text in ("採用元社", "秘密の応募枠"):
            self.assertIn(text, html)
        self.assertEqual(self.payload(html)["rows"], rows)
        for marker in ('id="lock"', "crypto.subtle", "sessionStorage", "decrypt", "__"):
            self.assertNotIn(marker, html)
        self.assertNotIn(" hidden>", html.split('id="app"')[1][:20])
        self.assertEqual(bv.build_html(rows, groups, companies, password="").count("id=\"lock\""), 0)

    def test_plaintext_escapes_script_close(self):
        rows, groups, companies, _ = self.built()
        html = bv.build_html(rows, groups, companies)
        self.assertEqual(html.count("</script>"), 2)

    def test_build_html_ignores_env(self):
        with mock.patch.dict(os.environ, {bv.PASSWORD_ENV: "x"}):
            self.assertIn("採用元社", bv.build_html(*self.built()[:3]))


class MainOutputsTest(unittest.TestCase):
    def run_main(self, args, env_password):
        env = {k: v for k, v in os.environ.items() if k != bv.PASSWORD_ENV}
        if env_password is not None:
            env[bv.PASSWORD_ENV] = env_password
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / "companies").mkdir()
            (data / "recruitment_cases").mkdir()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(bv, "DATA_DIR", data), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = bv.main(args)
        return code, err.getvalue()

    def test_without_password_only_plain_is_written(self):
        plain = bv.WORKSPACE_ROOT / "tmp" / "test_viewer_plain" / "index.html"
        enc = bv.WORKSPACE_ROOT / "tmp" / "test_viewer_plain" / "enc.html"
        self.addCleanup(shutil.rmtree, plain.parent, True)
        for value in (None, ""):
            code, err = self.run_main(["--plain-output", str(plain), "--output", str(enc)], value)
            self.assertEqual(code, 0)
            self.assertIn(bv.PASSWORD_ENV, err)
            self.assertTrue(plain.exists())
            self.assertFalse(enc.exists())

    def test_with_password_writes_both(self):
        plain = bv.WORKSPACE_ROOT / "tmp" / "test_viewer_both" / "index.html"
        enc = bv.WORKSPACE_ROOT / "tmp" / "test_viewer_both" / "enc.html"
        self.addCleanup(shutil.rmtree, plain.parent, True)
        code, _ = self.run_main(["--plain-output", str(plain), "--output", str(enc)], "pw")
        self.assertEqual(code, 0)
        self.assertNotIn('id="lock"', plain.read_text(encoding="utf-8"))
        self.assertIn('id="lock"', enc.read_text(encoding="utf-8"))

    def test_guard_rejects_non_ignored_paths(self):
        in_repo = bv.WORKSPACE_ROOT / "scripts" / "_plain_guard_test.html"
        with tempfile.TemporaryDirectory() as outside:
            for path in (in_repo, Path(outside) / "index.html"):
                enc = Path(outside) / "enc.html"
                code, err = self.run_main(["--plain-output", str(path), "--output", str(enc)], "pw")
                self.assertEqual(code, 1)
                self.assertIn("git", err)
                self.assertFalse(path.exists())
                self.assertFalse(enc.exists())

    def test_defaults(self):
        self.assertEqual(bv.DEFAULT_OUTPUT, bv.WORKSPACE_ROOT / "site" / "index.html")
        self.assertEqual(bv.DEFAULT_PLAIN_OUTPUT, bv.WORKSPACE_ROOT / "tmp" / "viewer" / "index.html")
        self.assertTrue(bv.is_git_ignored(bv.DEFAULT_PLAIN_OUTPUT))


if __name__ == "__main__":
    unittest.main()
