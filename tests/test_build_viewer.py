import base64
import contextlib
import copy
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

    def test_cell_details_are_not_shared_between_columns(self):
        # build_html は URL・note を詳細の中で添字に置き換える。列間で同じ object を共有すると二重に置き換わり、
        # ポップアップに添字そのもの（数字）が出る。
        sl = slot(2028, ["A"], [(240000, "official")])
        sl["starting_salary"]["note"] = "月給の注記"
        sl["starting_salary_master"] = {"records": [rec({"monthly_total_yen": 300000}, "unofficial")], "note": "修士の注記"}
        rows, groups, companies = self.built([sl])
        for k in ("初任給", "修士初任給"):
            self.assertIsNot(groups[rows[0][-1]["g"]][str(COL[k])], groups[rows[0][-1]["g"]][str(COL["学士初任給"])])
        p = profile("P", "採用元社")
        p["basic_information"]["industry_classification_codes"] = {"records": [rec(["E_manufacturing"])]}
        p["basic_information"]["industry_classification_codes"]["note"] = "業種の注記"
        d = bv.profile_details(p)
        self.assertIsNot(d[COL["業種1位"]], d[COL["業種"]])
        html = bv.build_html(rows, groups, {"P": {str(k): v for k, v in d.items()}})
        data = json.loads(re.search(r'<script[^>]*id="data"[^>]*>(.*?)</script>', html, re.S)[1])
        self.assertTrue(all(isinstance(x, str) for x in data["notes"] + data["urls"]))
        for store in [*data["groups"], *data["companies"].values()]:
            for cd in store.values():
                for n in cd[1]:
                    self.assertIn("注記", data["notes"][n[1]])

    def test_build_html_does_not_mutate_inputs(self):
        # main は同じ入力で平文版と暗号化版を続けて作る。入力を書き換えると2回目（暗号化版）だけ添字が二重化する。
        sl = slot(2028, ["A"], [(240000, "official")])
        sl["starting_salary"]["note"] = "月給の注記"
        rows, groups, companies = self.built([sl])
        before = json.dumps([rows, groups, companies], ensure_ascii=False, sort_keys=True)
        first = bv.build_html(rows, groups, companies)
        self.assertEqual(json.dumps([rows, groups, companies], ensure_ascii=False, sort_keys=True), before)
        second = bv.build_html(rows, groups, companies)
        strip = lambda h: re.sub(r'"built_at":"[^"]*"', "", h)
        self.assertEqual(strip(first), strip(second))

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
        self.assertIsNone(cands[0][6])
        self.assertEqual(cands[0][7]["sourced_date"], "28/2/29")
        # 今年度が優先されるときは今年度の値のまま、前年度の候補だけがずらし版を持つ
        r = self.rows([cur, prev])[0]
        self.assertEqual(len(r[COL["最初の締切"]]), 3)
        cands, _ = self.cell_detail([cur, prev], "最初の締切")
        self.assertEqual([(c[1], c[6]) for c in cands], [(1, None), (2, "28/5/5")])

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

    def test_notice_on_accuracy_and_password_sharing(self):
        html = bv.build_html(*self.built([slot(2028, ["A"])]))
        pw = "出典には再配布できない情報が含まれるため、合言葉は他人に教えたり公開したりしないでください。"
        self.assertIn("調査は AI（GPT-6 luna）が自動で行ったもので、誤りを含むことがあります。", html)
        self.assertIn(pw, html)
        self.assertIn(pw, bv.LOCK_HTML)

    def test_header_click_sorts_and_popup_has_no_sort_buttons(self):
        html = bv.build_html(*self.built([slot(2028, ["A"])]))
        self.assertIn("function sortClick", html)
        self.assertIn("if(sortCol!==i){sortCol=i;sortDir=1}else if(sortDir===1)sortDir=-1;else sortCol=-1", html)
        self.assertIn("if(th.isConnected&&!th.querySelector('.salary-controls:hover'))openPop(th,headerPop(i))", html)
        # 列ラベル・グループ見出しは値を持たないので、記録の欄（表示中・記録表）を出さない
        self.assertIn("function headerPop(i){return p=>columnPop(p,D.full[i],D.desc[i].short,D.desc[i].definition)}", html)
        self.assertIn("openPop(th,p=>columnPop(p,name,GINFO[name]||''))", html)
        self.assertNotIn("shown:'—（記録なし）',target:'',records:[]", html)
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
            "application_slots": [slot(2028, ["A"], [(300000, "official")], name="X"),
                                  slot(2028, ["A"], [(310000, "official")], name="Y")]}])
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


class MergeIdenticalRowsTest(unittest.TestCase):
    def case(self, slots):
        return {"recruitment_year": 2028, "publishing_company_id": "P", "application_slots": slots}

    def raw(self):
        rows, groups, companies = bv._build_rows(PROFILES, [self.case([
            slot(2028, ["A"], [(300000, "official")], name="正式X"),
            slot(2028, ["A"], [(300000, "official")], name="正式Y")])])
        rows = bv.ViewerRows(rows)
        rows.variants = {mode: copy.deepcopy((list(rows), groups, companies)) for mode in ("00", "01", "10")}
        return rows, groups, companies

    def test_merges_at_build_time_and_preserves_first_position_and_sort_key(self):
        slots = [slot(2028, ["A"], [(300000, "official")], name="正式X"),
                 slot(2028, ["A"], [(310000, "official")], name="別枠"),
                 slot(2028, ["A"], [(300000, "official")], name="正式Y")]
        with mock.patch.object(bv, "load_slot_short_names", return_value={"P_2028": {"正式X": "X", "正式Y": "Y"}}):
            rows, groups, _ = bv.build_rows(PROFILES, [self.case(slots)])
        self.assertEqual([r[COL["応募枠"]][0] for r in rows], ["X／Y", "別枠"])
        self.assertEqual(rows[0][COL["応募枠"]][1], "X")
        self.assertEqual([s["name"] for s in rows[0][-1]["slots"]], ["正式X", "正式Y"])
        self.assertEqual(rows[0][-1]["title"], ["配属A", "正式X", "正式Y"])
        self.assertEqual(len(groups), 2)  # 統合して不要になった詳細もペイロードから除去
        for rs, gs, _ in rows.variants.values():
            self.assertEqual((len(rs), len(gs)), (2, 2))
            self.assertEqual([r[-1]["g"] for r in rs], [0, 1])

    def test_every_cell_component_and_column_is_compared_in_every_mode(self):
        for mode in ("11", "00", "01", "10"):
            for col in range(len(bv.COLUMNS)):
                if col == COL["応募枠"]:
                    continue
                for part, changed in enumerate(("違う表示", "違うキー", "非", "違う詳細", "修", ["別の日", "別のキー"])):
                    with self.subTest(mode=mode, column=bv.COLUMNS[col], part=part):
                        built = self.raw()
                        rs = built[0] if mode == "11" else built[0].variants[mode][0]
                        for r in rs:
                            r[col] = [*r[col], *([""] * max(0, 6 - len(r[col])))]
                        rs[1][col][part] = changed
                        self.assertEqual(len(bv.merge_identical_rows(*built)[0]), 2)

    def test_detail_only_differences_in_any_mode_prevent_merging(self):
        for mode in ("11", "00", "01", "10"):
            with self.subTest(mode=mode):
                built = self.raw()
                rs, gs, _ = built if mode == "11" else built[0].variants[mode]
                gs[rs[1][-1]["g"]][str(COL["学士初任給"])][1].append(["今年度", "別の注記"])
                self.assertEqual(len(bv.merge_identical_rows(*built)[0]), 2)

    def test_company_detail_only_difference_is_compared(self):
        built = self.raw()
        # セル本体は同じでも、企業側の根拠を覆う枠側の根拠が異なる場合。
        col = str(COL["従業員数"])
        built[1][1][col] = copy.deepcopy(built[2]["A"][col])
        built[1][1][col][1].append(["企業情報", "別の注記"])
        self.assertEqual(len(bv.merge_identical_rows(*built)[0]), 2)

    def test_panel_only_difference_is_not_lost(self):
        built = self.raw()
        built[0][1][-1]["windows"].append({"name": "募集回", "open": "—", "deadline": "—"})
        self.assertEqual(len(bv.merge_identical_rows(*built)[0]), 2)

    def test_different_company_ids_do_not_merge_even_if_names_match(self):
        profiles = {**PROFILES, "B": copy.deepcopy(PROFILES["A"])}
        profiles["B"]["company_id"] = "B"
        rows, _, _ = bv.build_rows(profiles, [self.case([slot(2028, ["A", "B"], name="同名枠")])])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][:-1], rows[1][:-1])
        self.assertEqual([r[-1]["p"] for r in rows], ["A", "B"])

    def test_different_recruitment_years_do_not_merge(self):
        a, b = self.case([slot(2028, ["A"])]), self.case([slot(2029, ["A"])])
        b["recruitment_year"] = 2029
        self.assertEqual(len(bv.build_rows(PROFILES, [a, b])[0]), 2)

    def test_detail_difference_from_real_records_does_not_merge(self):
        a, b = slot(2028, ["A"], [(300000, "official")], name="X"), slot(2028, ["A"], [(300000, "official")], name="Y")
        b["starting_salary"]["note"] = "Yだけの根拠"
        rows, _, _ = bv.build_rows(PROFILES, [self.case([a, b])])
        self.assertEqual(rows[0][COL["学士初任給"]], rows[1][COL["学士初任給"]])
        self.assertEqual(len(rows), 2)

    def test_all_names_original_cells_and_inventory_survive_abbreviation(self):
        names = ["正式X", "正式Y", "正式Z", "正式W"]
        case = self.case([slot(2028, ["A"], name=n) for n in names])
        case["application_slot_inventory"] = {"records": [
            {"slot_name": n, "application_year": 2028, "source": {"kind": "official", "url": "u" + str(i)}}
            for i, n in enumerate(names)], "note": "収録の補足"}
        shorts = dict(zip(names, ["短X", "短Y", "短Z", "短W"]))
        with mock.patch.object(bv, "load_slot_short_names", return_value={"P_2028": shorts}):
            built = bv.build_rows(PROFILES, [case])
        payload = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', bv.build_html(*built), re.S)[1])
        self.assertEqual(len(payload["rows"]), 1)
        r = payload["rows"][0]
        self.assertEqual(r[1][0], "短X ほか3枠")
        self.assertEqual(r[1][1], "短X")
        self.assertEqual([s["name"] for s in r[-1]["slots"]], names)
        self.assertEqual([s["cell"] for s in r[-1]["slots"]], [[shorts[n], shorts[n], "", n] for n in names])
        self.assertEqual([s["src"][0][1] for s in r[-1]["slots"]], ["u0", "u1", "u2", "u3"])
        for n in names:
            self.assertIn(n, r[1][3])
            self.assertIn(shorts[n], r[1][3])  # 既存の検索対象に、省略された短名もすべて残す
        if shutil.which("node"):
            html = bv.build_html(*built)
            text_fn = re.search(r"const text=r=>[^\n]+", html)[0]
            code = "const NC=%d,r=%s,txt=c=>c[0];%s;console.log(JSON.stringify(text(r)))" % (len(bv.COLUMNS), json.dumps(r), text_fn)
            hay = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)
            for n in names:
                self.assertIn(n.lower(), hay)
                self.assertIn(shorts[n].lower(), hay)

    @unittest.skipUnless(shutil.which("node"), "node is required for JS checks")
    def test_popup_lists_all_formal_names_even_without_inventory_records(self):
        names = ["正式X", "正式Y", "正式Z", "正式W"]
        built = bv.build_rows(PROFILES, [self.case([slot(2028, ["A"], name=n) for n in names])])
        blocks = EvidencePopupTest().block_data(built, "metricBlocks(r,CI('応募枠'))")
        self.assertEqual([b["shown"] for b in blocks], names)
        self.assertTrue(all(b["heading"] for b in blocks))
        self.assertTrue(all(not b["records"] for b in blocks))

    def test_previous_only_and_current_empty_slots_keep_visibility_and_names(self):
        case = self.case([slot(2027, ["A"], name="前年だけ"), slot(2028, ["A"], name="当年")])
        rows, _, _ = bv.build_rows(PROFILES, [case])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1][0], "前年だけ／当年")
        self.assertEqual(rows[0][1][2], "")
        self.assertEqual(rows.variants["00"][0][0][1][0], "当年")
        self.assertEqual(rows.variants["00"][0][0][1][1], "前年だけ")
        self.assertEqual([s["prev"] for s in rows[0][-1]["slots"]], [True, False])


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
        self.assertEqual(s["src"], [[2028, "http://x", {"source_kind": "official"}]])
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
        self.assertEqual(c[3:], ["月給額の記載がなく基本給のみ"])
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
        html = bv.build_html(rows, groups, companies)
        self.assertNotIn("基</span> 基本給のみ", html)
        self.assertNotIn("「基」付き", html)


class DisplayModesTest(unittest.TestCase):
    def built(self):
        cur = slot(2028, ["A"], [(300000, "unofficial"), (310000, "unofficial")],
                   [window("unofficial", "2028-04-01")])
        prev = slot(2027, ["A"], [(250000, "official")], [window("official", "2027-03-01")])
        cur["starting_salary_master"] = {"records": [rec({"monthly_total_yen": 280000})]}
        cur["starting_salary_doctor"] = {"records": [rec({"monthly_total_yen": 400000}, "unknown")]}
        prev["starting_salary_doctor"] = {"records": [rec({"base_salary_yen": 350000})]}
        profiles = {**PROFILES, "A": profile("A", "配属A")}
        profiles["A"]["basic_information"]["employee_count"] = {"records": [rec(200, "unknown")]}
        return bv.build_rows(profiles, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                        "application_slots": [cur, prev, slot(2027, ["A"], name="前年だけ")]}])

    def modes(self):
        built = self.built()
        return {"11": built, **built[0].variants}

    def test_four_priority_combinations_and_details(self):
        expected = {"11": (250000, "前"), "01": (300000, "非+"),
                    "10": (250000, "前"), "00": (None, "")}
        for mode, (rows, groups, _) in self.modes().items():
            c = rows[0][COL["学士初任給"]]
            self.assertEqual((c[1], c[2]), expected[mode])
            cd = groups[0].get(str(COL["学士初任給"]))
            candidates = cd[0] if cd else []
            self.assertEqual(sum(c[4] for c in candidates), int(c[1] is not None))
            if mode[0] == "0":
                self.assertTrue(all(c[1] != 2 for c in candidates))
            if mode[1] == "0":
                self.assertTrue(all(c[2] == "公" for c in candidates))
                self.assertEqual(rows[0][COL["従業員数"]][0], "—")

    def test_max_salary_recomputed_after_exclusion(self):
        for mode, (rows, groups, _) in self.modes().items():
            expected = 350000 if mode[0] == "1" else 400000 if mode[1] == "1" else 280000
            self.assertEqual(rows[0][COL["初任給"]][1], expected)
            self.assertEqual(rows[0][COL["初任給"]][4], "修" if mode == "00" else "博")
            self.assertTrue(groups[0][str(COL["初任給"])][0][0][4])

    def test_date_cells_and_candidates_follow_mode(self):
        for mode, (rows, groups, _) in self.modes().items():
            c = rows[0][COL["最初の締切"]]
            self.assertEqual(c[0], "27/3/1" if mode[0] == "1" else "28/4/1" if mode[1] == "1" else "—")
            self.assertEqual(len(c) == 6, mode[0] == "1")
            cd = groups[0].get(str(COL["最初の締切"]))
            if mode[0] == "0":
                self.assertTrue(all(c[1] != 2 for c in cd[0]) if cd else True)

    def test_previous_only_rows_are_hidden_by_render(self):
        built = self.built()
        html = bv.build_html(*built)
        condition = re.search(r"const available=D.rows.filter\((.*?)\);", html)[1]
        script = "const NC=%d,D={rows:%s};for(const previous of [true,false]){const view={previous};console.log(D.rows.filter(%s).length)}" % (
            len(bv.COLUMNS), json.dumps(built[0]), condition)
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), ["2", "1"])

    def test_payload_deltas_reconstruct_cells_and_details(self):
        built = self.built()
        data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',
                                   bv.build_html(*built), re.S)[1])
        for mode, values in self.modes().items():
            rows, groups, companies = copy.deepcopy(values)
            bv.unify_decimals(rows, groups, companies)
            patch = data["variants"].get(mode, {}).get("rows", {})
            for i, r in enumerate(rows):
                actual = list(data["rows"][i])
                for j, c in patch.get(str(i), {}).items():
                    actual[int(j)] = c
                self.assertEqual(actual[:len(bv.COLUMNS)], r[:len(bv.COLUMNS)])
            for field, expected in (("groups", dict(enumerate(groups))), ("companies", companies)):
                delta = data["variants"].get(mode, {}).get(field, {})
                for k, store in expected.items():
                    base = data[field][k] if field == "groups" else data[field].get(k, {})
                    actual = copy.deepcopy({**base, **delta.get(str(k), {})})
                    actual = {k: cd for k, cd in actual.items() if cd is not None}
                    for cd in actual.values():
                        for c in cd[0]:
                            c[3] = data["urls"][c[3]] if c[3] >= 0 else ""
                        for n in cd[1]:
                            n[1] = data["notes"][n[1]]
                    self.assertEqual(actual, store)
        self.assertNotIn("11", data["variants"])

    def test_expanded_degree_hides_monthly_column(self):
        html = bv.build_html(*self.built())
        expression = re.search(r"const shown=i=>\{(.*?)\};", html)[1]
        script = "const D={fold:%s},fold={e:true,d:[false,false,true]};const shown=i=>{%s};console.log(JSON.stringify(D.fold.map((_,i)=>shown(i))))" % (
            json.dumps([bv.fold_role(c) for c in bv.COLUMNS]), expression)
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        shown = json.loads(result.stdout)
        self.assertFalse(shown[COL["博士初任給"]])
        self.assertTrue(shown[COL["博士基本給"]])
        self.assertTrue(shown[COL["修士初任給"]])
        self.assertIn("DEGREE_NAMES.indexOf(name)", html)  # 表示中のグループ見出しに閉じる操作がある

    def test_degree_switch_is_scoped_to_summary_header(self):
        html = bv.build_html(*self.built())
        toolbar = re.search(r'<div class="tools">(.*?)</header>', html, re.S)[1]
        self.assertNotIn("初任給", toolbar)
        self.assertNotIn('id="degrees"', html)
        self.assertNotIn(".degrees", html)
        self.assertIn("if(x.r==='max'){const controls=", html)
        self.assertIn("controls.append(degreeButton(),tg('▸'", html)
        self.assertIn("見出しで選んだ学位", bv.column_descriptions()["初任給"]["short"])
        self.assertIn("クリックで学位を切り替え（学士→修士→博士→最大）", html)
        self.assertIn("flex-direction:column", html)
        self.assertNotIn("function degreeSelect", html)

    def test_degree_button_cycles_and_stops_header_events(self):
        html = bv.build_html(*self.built())
        fn = re.search(r"function degreeButton\(\)\{.*?return b\}", html, re.S)[0]
        script = """
const DEGREE_NAMES=['学士','修士','博士'];let view={degree:'学士'},saved=0,stopped=0;
function el(tag,text,cls){return {tag,text,cls,attrs:{},events:{},
 setAttribute(k,v){this.attrs[k]=v},addEventListener(k,f){this.events[k]=f}}}
function setView(f){f();saved++}
%s
const initial=degreeButton(),states=[];
for(const f of Object.values(initial.events))f({stopPropagation(){stopped++}});
if(saved!==0)throw Error('pointer or key changed the view');
for(let i=0;i<4;i++){degreeButton().onclick({stopPropagation(){stopped++}});
 const b=degreeButton();states.push([b.text,b.attrs['aria-label']])}
console.log(JSON.stringify({initial:initial.text,states,saved,stopped,events:Object.keys(initial.events)}));
""" % fn
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "initial": "学士", "states": [[d, "初任給の学位：" + d] for d in ["修士", "博士", "最大", "学士"]],
            "saved": 4, "stopped": 8, "events": ["pointerdown", "pointerup", "keydown", "contextmenu"]})

    def test_shift_switch_is_page_wide_in_toolbar(self):
        cur = slot(2028, ["A"])
        prev = slot(2027, ["A"], windows=[window("official", "2027-04-01", opens=["2027-03-01"])])
        built = bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                         "application_slots": [cur, prev]}])
        shifted_columns = {bv.COLUMNS[i] for r in built[0] for i, c in enumerate(r[:-1]) if len(c) > 5}
        self.assertEqual(shifted_columns, {"最初の募集開始", "最初の締切"})
        self.assertTrue(all(bv.header_path(c) == ["日程"] for c in shifted_columns))
        html = bv.build_html(*built)
        toolbar = re.search(r'<div class="tools">(.*?)</header>', html, re.S)[1]
        self.assertIn('id="shift"', toolbar)
        self.assertIn('id="shift-wrap"', toolbar)
        self.assertIn('title="前年度の募集枠の行と値を表示"', toolbar)
        self.assertIn("document.getElementById('shift-wrap').hidden=!view.previous", html)
        self.assertIn("shx.onchange=()=>setView(()=>{view.shift=shx.checked})", html)
        self.assertNotIn("shiftToggle", html)
        self.assertNotIn("header-shift", html)
        self.assertIn("右パネルも同じ日程", html)

    def test_panel_window_dates_shift_independently(self):
        html = bv.build_html(*self.built())
        fn = re.search(r"function windowDate\(w,k\)\{[^\n]+\}", html)[0]
        script = """
let shiftOn=false;
%s
const w={open:'後日案内',open1:'後日案内',deadline:'27/4/1',deadline1:'28/4/1'};
const off=['open','deadline'].map(k=>windowDate(w,k));shiftOn=true;
const on=['open','deadline'].map(k=>windowDate(w,k));
console.log(JSON.stringify({off,on,onlyDeadline:windowDate({deadline:'27/5/1',deadline1:'28/5/1'},'deadline'),
 current:windowDate({open:'28/3/1'},'open'),missing:windowDate({},'deadline')}));
""" % fn
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "off": [["後日案内", False], ["27/4/1", False]],
            "on": [["後日案内", False], ["28/4/1", True]],
            "onlyDeadline": ["28/5/1", True], "current": ["28/3/1", False], "missing": [None, False]})
        self.assertIn("[['開始','open'],['締切','deadline']]", html)
        self.assertIn("const [t,on]=windowDate(w,k)", html)
        self.assertIn("v.append(valNode(c))", html)
        self.assertIn("v.append(vnode(c))", html)

    def test_official_filter_inherits_window_source(self):
        w = window("official", "2028-03-01")
        del w["application_deadline_date"]["records"][0]["source"]
        self.assertEqual(len(bv.official_records(w)["application_deadline_date"]["records"]), 1)
        w["source"]["kind"] = "unknown"
        self.assertEqual(bv.official_records({"records": [w]})["records"], [])


class EvidencePopupTest(unittest.TestCase):
    def built(self, sl=None):
        return bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P",
                                        "application_slots": [sl or slot(2027, ["A"], [(307000, "official")])]}])

    def html(self):
        return bv.build_html(*self.built())

    def node(self, script):
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_salary_evidence_keeps_named_kind_degree_and_breakdown(self):
        sl = slot(2027, ["A"])
        sl["starting_salary"] = {"records": [rec({"monthly_total_yen": 307000, "base_salary_yen": 250000,
                                                 "fixed_overtime_yen": 57000, "fixed_overtime_hours": 20})]}
        rows, groups, companies = self.built(sl)
        metadata = groups[0][str(COL["学士初任給"])][0][0][7]
        self.assertEqual(metadata, {"degree": "学士", "salary_kind": "monthly_total", "monthly_total_yen": 307000,
                                    "base_salary_yen": 250000, "fixed_overtime_yen": 57000, "fixed_overtime_hours": 20})
        self.assertEqual(groups[0][str(COL["初任給"])][0][0][7], metadata)
        self.assertEqual(groups[0][str(COL["学士固定残業代"])][0][0][7], metadata)
        payload = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',
                                      bv.build_html(rows, groups, companies), re.S)[1])
        self.assertEqual(payload["groups"][0][str(COL["学士初任給"])][0][0][7], metadata)

    def test_base_only_and_missing_breakdown_are_not_invented(self):
        sl = slot(2027, ["A"])
        sl["starting_salary"] = {"records": [rec({"base_salary_yen": 408500})]}
        metadata = self.built(sl)[1][0][str(COL["学士初任給"])][0][0][7]
        self.assertEqual(metadata["salary_kind"], "base_only")
        self.assertIsNone(metadata["monthly_total_yen"])
        self.assertIsNone(metadata["fixed_overtime_yen"])
        self.assertIsNone(metadata["fixed_overtime_hours"])
        # 企業指標には応募枠の年度や推測した測定年を付けない。
        candidate = bv.profile_details(PROFILES["A"])[COL["従業員数"]][0][0]
        self.assertEqual((candidate[1], candidate[7]), (0, {}))

    def test_shifted_date_keeps_sourced_date_and_retrieval(self):
        w = window("official", "2027-03-15")
        w["application_deadline_date"]["records"][0]["retrieval"] = {
            "method": "host_fetch", "fetched_at": "2026-10-11T00:00:00Z"}
        candidate = self.built(slot(2027, ["A"], windows=[w]))[1][0][str(COL["最初の締切"])][0][0]
        self.assertEqual(candidate[6], "28/3/15")
        self.assertIn("27/3/15", candidate[0])
        self.assertIn("取得 2026-10-11", candidate[0])
        self.assertEqual(candidate[7], {"sourced_date": "27/3/15", "retrieval": "取得 2026-10-11"})
        html = self.html()
        self.assertIn("予想 ", html)
        self.assertIn("を+1年・未確定", html)
        self.assertNotIn("li.append(el('span','採用'", html)

    def test_popup_structure_and_slot_remote_removal(self):
        html = self.html()
        self.assertIn("p.before(h)", html)  # 固定ヘッダーはスクロール本文の兄弟
        self.assertIn("#pop .pb{min-height:0;overflow:auto", html)
        self.assertIn("new ResizeObserver(()=>{measureNotes();placePop()}).observe(pop)", html)
        self.assertIn("pop.addEventListener('toggle',()=>{measureNotes();placePop()},true)", html)
        self.assertIn("document.addEventListener('scroll'", html)
        self.assertIn("max-height:calc(100dvh - 16px)", html)
        self.assertIn("['値','対象','種類','出典']", html)
        self.assertIn("定義 ▸", html)
        self.assertIn("width:480px;max-width:calc(100vw - 16px)", html)
        for obsolete in ("function slotPop", "function selectionPop", "function evidenceGrid", "function otherRecords",
                         "#pop .shown", "#pop .egrid", "sel-evidence", "表示した結果"):
            self.assertNotIn(obsolete, html)
        blocks = html.split("function metricBlocks", 1)[1].split("function cellPop", 1)[0]
        self.assertNotIn("リモート", blocks)
        self.assertIn("blocks.forEach(b=>renderMetricBlock(p,b))", html)
        self.assertIn("if(j===5)", html)
        self.assertIn("record.chosen?'● '", html)
        self.assertNotIn("リモート", bv.column_descriptions()["応募枠"]["short"])
        self.assertNotIn("リモート", str(bv.column_descriptions()["応募枠"]["definition"]))
        self.assertNotIn("remote", self.built()[1][0])
        self.assertIn("リモート制度", bv.COLUMNS)
        self.assertIn("売上が一番目に大きい", bv.column_descriptions()["業種1位"]["short"])

    def test_header_definition_is_inline_and_opens_below_title(self):
        fn = self.html().split("function popHead", 1)[1].split("// 列ラベル", 1)[0]
        result = self.node("""
class Element {
 constructor(tag,text,cls){this.tag=tag;this.textContent=text;this.cls=cls;this.children=[];this.attrs={}}
 append(...xs){this.children.push(...xs)}
 before(e){this.header=e}
 setAttribute(k,v){this.attrs[k]=v}
}
const el=(...args)=>new Element(...args),pop=el('div');let placed=0;
const placePop=()=>placed++,closePop=()=>{};
function popHead%s
const p=el('div');popHead(p,'初任給','会社 ・ 枠',['定義本文']);
const t=p.header.children[0],row=t.children[1],button=row.children[1],definition=t.children[2];
const initial={context:t.children[0].textContent,title:row.children[0].textContent,
 rowClass:row.cls,button:button.textContent,hidden:definition.hidden,expanded:button.attrs['aria-expanded']};
button.onclick();const opened={hidden:definition.hidden,expanded:button.attrs['aria-expanded'],text:definition.children[0].textContent};
button.onclick();const closed=definition.hidden;
const plain=el('div');popHead(plain,'列ラベル');
console.log(JSON.stringify({initial,opened,closed,placed,plainCount:plain.header.children[0].children[0].children.length}));
""" % fn)
        self.assertEqual(result["initial"], {"context": "会社 ・ 枠", "title": "初任給", "rowClass": "title-row",
                                             "button": "定義 ▸", "hidden": True, "expanded": "false"})
        self.assertEqual(result["opened"], {"hidden": False, "expanded": "true", "text": "定義本文"})
        self.assertTrue(result["closed"])
        self.assertEqual((result["placed"], result["plainCount"]), (2, 1))

    def test_notes_measure_rendered_lines_and_toggle_without_text_heuristics(self):
        html = self.html()
        fn = html.split("function measureNotes", 1)[1].split("function selectionTypes", 1)[0]
        result = self.node("""
const el=()=>({setAttribute(k,v){this[k]=v},remove(){this.parent.button=null}});
let placed=0;const placePop=()=>placed++,getComputedStyle=()=>({lineHeight:'20px'});
function note(height,width=200){const classes=new Set(),parent={button:null,querySelector(){return this.button}};
 return {dataset:{},parentElement:parent,height,width,
 classList:{remove(c){classes.delete(c)},toggle(c,on){on?classes.add(c):classes.delete(c)},contains(c){return classes.has(c)}},
 getBoundingClientRect(){return {width:this.width,height:this.width?(classes.has('clamped')?40:this.height):0}},
 after(button){parent.button=button;button.parent=parent}}}
// 同じ文字数でも実測が違えば異なる扱い。非表示ブロックは幅0。
const short=note(40),long=note(60),hidden=note(80,0),notes=[short,long,hidden];
const pop={querySelectorAll(){return notes}};
function measureNotes%s
measureNotes();const initial={shortButton:!!short.parentElement.button,longClamped:long.classList.contains('clamped'),
 label:long.parentElement.button.textContent,hiddenMeasured:!!hidden.dataset.measuredWidth};
long.parentElement.button.onclick();const opened={clamped:long.classList.contains('clamped'),label:long.parentElement.button.textContent};
measureNotes();const staysOpen=!long.classList.contains('clamped');long.parentElement.button.onclick();
const closed={clamped:long.classList.contains('clamped'),label:long.parentElement.button.textContent};
hidden.width=200;measureNotes();const newlyVisible=hidden.classList.contains('clamped');
long.width=300;long.height=40;short.width=100;short.height=60;measureNotes();
console.log(JSON.stringify({initial,opened,staysOpen,closed,newlyVisible,placed,
 resizedShort:short.classList.contains('clamped'),resizedLongButton:!!long.parentElement.button}));
""" % fn)
        self.assertEqual(result["initial"], {"shortButton": False, "longClamped": True, "label": "続き ▸", "hiddenMeasured": False})
        self.assertEqual(result["opened"], {"clamped": False, "label": "閉じる ▴"})
        self.assertEqual(result["closed"], {"clamped": True, "label": "続き ▸"})
        self.assertTrue(result["staysOpen"])
        self.assertTrue(result["newlyVisible"])
        self.assertTrue(result["resizedShort"])
        self.assertFalse(result["resizedLongButton"])
        self.assertEqual(result["placed"], 2)
        self.assertIn("pop.hidden=false;measureNotes();placePop()", html)
        self.assertIn("-webkit-line-clamp:2", html)

    def test_target_words_use_case_year_not_calendar_year(self):
        fn = re.search(r"function targetOf\(r,y\)\{[^\n]+\}", self.html())[0]
        grad = re.search(r"const gradYear=[^\n]+", self.html())[0]
        result = self.node("const NC=0,r=[{slot:{ry:2028}}];" + grad + fn +
                           ";console.log(JSON.stringify([0,1,2].map(y=>targetOf(r,y))))")
        self.assertEqual(result, ["", "28卒", "27卒・前年度"])

    def test_selection_focus_is_the_clicked_types_and_empty_title_is_not_used(self):
        html = self.html()
        fn = html.split("function selectionTypes", 1)[1].split("function metricBlockData", 1)[0]
        script = "const D=%s,CI=n=>D.columns.indexOf(n);const r=D.columns.map(()=>['—']);%s;" % (
            json.dumps({"columns": bv.COLUMNS, "fold": [bv.fold_role(c) for c in bv.COLUMNS]}),
            "function selectionTypes" + fn)
        result = self.node(script + "const empty=selectionTypes(r,CI('選考'));r[CI('ケース')]=['○'];r[CI('ES')]=['○'];"
                           "console.log(JSON.stringify({empty,rare:selectionTypes(r,CI('選考')),es:selectionTypes(r,CI('ES'))}))")
        self.assertEqual(result, {"empty": [], "rare": ["ケース"], "es": ["ES"]})
        self.assertIn("popHead(p,selection?'選考':blocks[0].name", html)
        self.assertNotIn("D.full[i]+'：'+", html)
        self.assertIn("b.collapsed=focus.length?!focus.includes(n):j>0", html)
        self.assertIn("names=['工程',...D.columns.filter", html)

    def block_data(self, built, expression, shift=False):
        html = bv.build_html(*built)
        payload = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)[1])
        functions = html.split("const SOURCE_WORDS=", 1)[1].split("function cellPop", 1)[0]
        cell_detail = html.split("function cellDetail", 1)[1].split("function safeUrl", 1)[0]
        return self.node("const D=" + json.dumps(payload, ensure_ascii=False) + ";const NC=D.columns.length,r=D.rows[0],"
                         "CI=n=>D.columns.indexOf(n),unitOf=i=>{const m=/（(.+)）$/.exec(D.full[i]);return m?m[1]:''},"
                         "view={degree:'学士'},shiftOn=" + str(shift).lower() + ";"
                         "const sv=c=>shiftOn&&c[5]?c[5]:null,txt=c=>(sv(c)||c)[0];"
                         "function cellDetail" + cell_detail + "const SOURCE_WORDS=" + functions +
                         ";console.log(JSON.stringify(" + expression + "))")

    def test_common_block_salary_notes_and_company_target(self):
        sl = slot(2027, ["A"])
        sl["starting_salary"] = {"records": [rec({"base_salary_yen": 307000})], "note": "前年の記録"}
        built = self.built(sl)
        salary, company = self.block_data(built, "[metricBlocks(r,CI('初任給'))[0],metricBlocks(r,CI('従業員数'))[0]]")
        self.assertEqual(salary["shown"], "30.7 万円/月")
        self.assertEqual(salary["target"], "27卒・前年度")
        self.assertEqual(salary["records"][0]["value"], "30.7 万円/月")
        self.assertEqual(salary["records"][0]["kind"], "公式")
        self.assertTrue(salary["records"][0]["chosen"])
        self.assertIn("基本給のみ", salary["records"][0]["extra"][0])
        self.assertIn("記載なし", salary["records"][0]["extra"][1])
        self.assertEqual(salary["notes"], [["27卒・前年度について", "前年の記録"]])
        self.assertEqual(company["target"], "")
        self.assertEqual(company["records"][0]["target"], "")
        self.assertEqual(company["records"][0]["value"], "100 人")
        current_slot = slot(2028, ["A"])
        current_slot["starting_salary"] = {"records": [rec({"base_salary_yen": 307000})], "note": "当年の記録"}
        current = self.block_data(self.built(current_slot), "metricBlocks(r,CI('初任給'))[0]")
        self.assertEqual(current["notes"], [["28卒について", "当年の記録"]])

    def test_common_date_rows_are_sourced_even_when_shown_value_is_prediction(self):
        w = window("official", "2027-03-15")
        w["window_name"] = "春募集"
        w["application_deadline_date"]["records"][0]["retrieval"] = {
            "method": "host_fetch", "fetched_at": "2026-10-11T00:00:00Z"}
        built = self.built(slot(2027, ["A"], windows=[w]))
        block = self.block_data(built, "metricBlocks(r,CI('最初の締切'))[0]", shift=True)
        self.assertEqual(block["shown"], "予想 28/3/15（27/3/15 を+1年・未確定）")
        self.assertEqual(block["records"][0]["value"], "27/3/15")
        self.assertEqual(block["records"][0]["extra"], ["募集回：春募集", "取得 2026-10-11"])
        later = {"source": {"kind": "official", "url": "u"}, "window_name": "夏募集",
                 "application_deadline_date": {"announced_later": {
                     "source": {"kind": "official", "url": "u"},
                     "retrieval": {"method": "host_fetch", "fetched_at": "2026-10-11T00:00:00Z"}}}}
        block = self.block_data(self.built(slot(2027, ["A"], windows=[later])),
                                "metricBlocks(r,CI('最初の締切'))[0]", shift=True)
        self.assertEqual(block["shown"], "後日案内")
        self.assertEqual(block["records"][0]["value"], "後日案内")
        self.assertTrue(block["records"][0]["chosen"])

    def test_selection_blocks_keep_process_records_and_clicked_type_first(self):
        sl = slot(2027, ["A"])
        sl["selection_process"] = {
            "process": {"records": [rec({"stages": [{"steps": [{"label": "ES"}]},
                                                   {"steps": [{"label": "ケース面接"}]}]})], "note": "工程の補足"},
            "has_case_interview": {"records": [rec(True), rec(False, "unofficial")], "note": "ケースの補足"}}
        blocks = self.block_data(self.built(sl), "metricBlocks(r,CI('ケース'))")
        self.assertEqual(len(blocks), 11)
        self.assertEqual(blocks[0]["name"], "ケース面接")
        self.assertFalse(blocks[0]["collapsed"])
        self.assertEqual(blocks[0]["shown"], "あり")
        self.assertEqual([r["value"] for r in blocks[0]["records"]], ["あり", "なし"])
        self.assertTrue(all(b["collapsed"] for b in blocks[1:]))
        process = next(b for b in blocks if b["name"] == "選考工程")
        self.assertEqual(process["shown"], "ES → ケース面接")
        self.assertEqual(process["records"][0]["value"], "ES → ケース面接")
        self.assertEqual(process["notes"], [["工程 27卒・前年度について", "工程の補足"]])
        self.assertEqual(next(b for b in blocks if b["name"] == "ES提出")["shown"], "—（記録なし）")

    def test_shared_renderer_keeps_five_rows_and_collapses_remainder(self):
        fn = self.html().split("function renderMetricBlock", 1)[1].split("function selectionTypes", 1)[0]
        result = self.node("""
class Element {
 constructor(tag,text,cls){this.tag=tag;this.text=text;this.cls=cls;this.children=[]}
 append(...xs){this.children.push(...xs)}
 cloneNode(deep){const e=new Element(this.tag,this.text,this.cls);if(deep)e.children=this.children.map(x=>x.cloneNode(true));return e}
}
const el=(tag,text,cls)=>new Element(tag,text,cls),sourceLink=url=>el('a',url);
function renderMetricBlock%s
const p=el('div'),record={value:'値',target:'28卒',kind:'公式',url:'https://example.org',extra:['基本給 記載なし','固定残業代 記載なし']};
renderMetricBlock(p,{name:'指標',shown:'値',target:'',records:Array.from({length:8},(_,j)=>({...record,chosen:j===0})),notes:[['対象年度','本文']]});
const walk=(e,tag)=>[...(e.tag===tag?[e]:[]),...e.children.flatMap(c=>walk(c,tag))];
const tables=walk(p,'table'),details=walk(p,'details'),chosen=walk(p,'tr').filter(e=>e.cls==='chosen');
const empty=el('div');renderMetricBlock(empty,{name:'指標',shown:'—（記録なし）',target:'',records:[],notes:[],collapsed:true});
const merged=el('div');renderMetricBlock(merged,{name:'応募枠',shown:'ビジネス職',target:'28卒',heading:true,merged:true,records:[],notes:[]});
console.log(JSON.stringify({rows:tables.map(t=>walk(t,'tbody')[0].children.length),headers:tables.map(t=>walk(t,'th').map(x=>x.text)),
 summary:walk(details[0],'summary')[0].text,chosen:chosen.length,mark:chosen[0].children[0].children[0].text,
 headings:walk(p,'h3').map(e=>e.text),extra:walk(p,'div').filter(e=>e.cls==='record-extra')[0].text,emptyTag:empty.children[0].tag,emptyValue:walk(empty,'td')[0].text,
 displayed:walk(p,'div').filter(e=>e.cls==='displayed').length,mergedHeadings:walk(merged,'h3').map(e=>e.text),
 mergedDisplayed:walk(merged,'div').filter(e=>e.cls==='displayed').length}));
""" % fn)
        self.assertEqual(result["headings"], [])
        self.assertEqual(result["rows"], [5, 3])
        self.assertEqual(result["headers"], [["値", "対象", "種類", "出典"]] * 2)
        self.assertEqual(result["summary"], "ほか 3件 ▸")
        self.assertEqual((result["chosen"], result["mark"]), (1, "● 値"))
        self.assertEqual(result["extra"], "基本給 記載なし・固定残業代 記載なし")
        self.assertEqual((result["emptyTag"], result["emptyValue"]), ("details", "—（記録なし）"))
        self.assertEqual(result["displayed"], 1)
        # 統合行の枠は見出しに年度を含め、「表示中」を重ねて出さない
        self.assertEqual(result["mergedHeadings"], ["応募枠：ビジネス職（28卒）"])
        self.assertEqual(result["mergedDisplayed"], 0)

    def test_inventory_uses_same_block_with_formal_names_and_assignment_only(self):
        sl = slot(2027, ["A", "missing-profile"], name="正式なコース")
        case = {"recruitment_year": 2028, "publishing_company_id": "P", "application_slots": [sl],
                "application_slot_inventory": {"records": [
                    {"application_year": 2027, "slot_name": "正式なコース", "source": {"kind": "official", "url": "https://example.org"}},
                    {"application_year": 2028, "slot_name": "正式なコース", "source": {"kind": "unofficial", "url": "https://other.example"}}],
                    "note": "収録の理由"}}
        block = self.block_data(bv.build_rows(PROFILES, [case]), "metricBlocks(r,CI('応募枠'))[0]")
        self.assertEqual([r["target"] for r in block["records"]], ["27卒・前年度", "28卒"])
        self.assertEqual(block["target"], "27卒・前年度")
        self.assertEqual([r["value"] for r in block["records"]], ["正式なコース", "正式なコース"])
        self.assertEqual([r["chosen"] for r in block["records"]], [True, False])
        self.assertEqual(block["extra"], "配属先：配属A、missing-profile")
        self.assertEqual(block["notes"], [["応募枠の収録について", "収録の理由"]])
        self.assertNotIn("remote", str(block))

    def test_viewport_clamp_content_resize_and_out_of_view_anchor(self):
        fn = self.html().split("function placePop(){", 1)[1].split("function closePop", 1)[0]
        result = self.node("""
let innerWidth=1440,innerHeight=720,closed=0;
let rect={left:1430,right:1460,top:650,bottom:670};
const pop={hidden:false,offsetWidth:420,offsetHeight:704,style:{}},
 popAnchor={isConnected:true,getBoundingClientRect(){return rect},closest(){return null}};
function matchMedia(){return {matches:false}}function closePop(){closed++;pop.hidden=true}
function placePop(){%s
placePop();const first={...pop.style};pop.offsetHeight=350;placePop();const changed={...pop.style};
rect={left:0,right:40,top:721,bottom:740};placePop();
console.log(JSON.stringify({first,changed,closed}));
""" % fn)
        self.assertEqual(result, {"first": {"left": "1012px", "top": "8px"},
                                  "changed": {"left": "1012px", "top": "296px"}, "closed": 1})

    def test_row_cache_reuses_nodes_and_preserves_selection_column(self):
        html = self.html()
        select = html.split("function selectRow(tr){", 1)[1].split("const hd=", 1)[0]
        render = html.split("function render(){", 1)[1].split("function buildRow", 1)[0]
        result = self.node(r"""
const NC=2,a=[["A",2],["a",0],{slot:{prev:false}}],b=[["B",1],["b",0],{slot:{prev:false}}];
a.hay='a';b.hay='b';const D={rows:[a,b],built_at:'test'},view={previous:true};
let selected=a,selCol=1,selTd=null,selTr=null,sortCol=-1,sortDir=1,query='',builds=0;
const rowCache=new Map(),info={},tb={replaceChildren(f){this.rows=f.rows}},
 document={createDocumentFragment(){return {rows:[],appendChild(tr){this.rows.push(tr)}}}};
function closePop(){}function applyFold(){}function renderPanel(){}function narrow(){return false}
let hoverTr=null;function showHover(){} // 帯の描画は DOM が要るので、ここでは行の再利用・選択だけを見る
function groupRows(rows){return rows}const homeOf=new Map();function markRows(){}function applySpans(){}  // 会社行への収納・配属先セルの結合は別のテスト
function key(c){return c[1]}function cmp(a,b){return a-b}
function buildRow(r){builds++;return {r,cells:[0,1].map(()=>({classList:{values:new Set(),
 add(v){this.values.add(v)},remove(v){this.values.delete(v)}}})),removeAttribute(){delete this.className}}}
function selectRow(tr){%s
function render(){%s
render();const first=tb.rows[0];sortCol=0;render();const reused=tb.rows[1]===first;
query='b';render();const moved=selected===b&&selTd===tb.rows[0].cells[1]&&!first.cells[1].classList.values.has('selc');
query='';render();selCol=-1;selectRow(first);const noCell=selTd===null;
console.log(JSON.stringify({builds,reused,moved,noCell,oldRowUnselected:!tb.rows[0].className}));
""" % (select, render))
        self.assertEqual(result, {"builds": 2, "reused": True, "moved": True,
                                  "noCell": True, "oldRowUnselected": True})

    def test_cell_click_avoids_render_and_content_changes_invalidate_cache(self):
        html = self.html()
        handler = html.split("tr.onclick=e=>", 1)[1].split("r.slice(0,NC)", 1)[0]
        self.assertNotIn("render();", handler)
        self.assertIn("closePop();", handler)
        self.assertIn("selectRow(tr);renderPanel();", handler)
        for begin, end in [("function setFold", "function tg"),
                           ("function displayRows", "function setView")]:
            self.assertIn("rowCache.clear()", html.split(begin, 1)[1].split(end, 1)[0])

    def test_requested_pointer_leave_behavior_is_preserved(self):
        html = self.html()
        self.assertIn("leaveTimer=setTimeout(closePop,250)", html)
        self.assertIn("anchor.addEventListener('pointerleave',leaveClose)", html)
        self.assertIn("pop.addEventListener('pointerleave',leaveClose)", html)
        self.assertIn("if(e.pointerType==='mouse')cancelLeave()", html)
        self.assertNotIn("popMode", html)


@unittest.skipUnless(shutil.which("node"), "node is required for JS checks")
class CompanyCollapseTest(unittest.TestCase):
    """配属先ごとの会社行への収納・展開。ブラウザーの代わりに node で、実際の描画・会社行・ポップアップの関数に最小限の DOM を与えて確かめる。"""

    ALL_OPEN = "{getItem(){return '[\"P\",\"Q\",\"R\",\"T\",\"U\"]'},setItem(){}}"

    PROFILES = {**PROFILES, **{k: profile(k, "採用元" + k) for k in "FGQRTU"}, "S": profile("S", "株式会社採用元S")}

    @staticmethod
    def sal(name, yen=None, deadline=None, url="u", to="A"):
        sl = slot(2028, [to], name=name, windows=[window("official", deadline)] if deadline else ())
        if yen:
            sl["starting_salary"] = {"records": [{"value": {"monthly_total_yen": yen}, "source": {"kind": "official", "url": url}}]}
        return sl

    @staticmethod
    def case(pub, *slots):
        return {"recruitment_year": 2028, "publishing_company_id": pub, "application_slots": list(slots)}

    def built(self, *cases):
        with mock.patch.object(bv, "load_slot_short_names", return_value={}):
            return bv.build_rows(self.PROFILES, list(cases))

    def main_built(self):
        s = self.sal  # 配属先は採用元と同じ会社（P, Q, R, T, U の5つ）
        return self.built(
            self.case("P", s("甲", 300000, to="P"), s("乙", 250000, to="P"), s("丙", 350000, to="P")),
            self.case("Q", s("Q甲", 280000, to="Q"), s("Q乙", 400000, to="Q")),
            self.case("R", s("R甲", 270000, to="R")),
            self.case("T", s("T甲", to="T"), s("T乙", 220000, to="T")),
            self.case("U", s("U甲", deadline="2028-04-01", to="U"), s("U乙", deadline="2028-05-01", to="U")))

    def filter_built(self):
        s = self.sal  # 配属先は A だけ。甲コースと乙コースは月給の値・根拠が同じで、締切だけが違う（統合されない）
        return self.built(self.case("F", s("甲コース", 300000, "2028-04-01"), s("乙コース", 300000, "2028-05-01"),
                                    s("丙別枠", 350000)))

    def values_built(self):
        s = self.sal  # 配属先ごとに同じ月給の枠が2つ（G1:30 G2:20 G3:25）、月給が混ざる配属先（G4:30と10）、1行だけの配属先（G5:15）
        return self.built(
            self.case("F", s("G1a", 300000, "2028-04-01", to="G1"), s("G1b", 300000, "2028-05-01", to="G1")),
            self.case("G", s("G2a", 200000, "2028-04-01", to="G2"), s("G2b", 200000, "2028-05-01", to="G2")),
            self.case("Q", s("G3a", 250000, "2028-04-01", to="G3"), s("G3b", 250000, "2028-05-01", to="G3")),
            self.case("R", s("G4a", 300000, "2028-04-01", to="G4"), s("G4b", 100000, "2028-05-01", to="G4")),
            self.case("T", s("G5a", 150000, to="G5")))

    VALUES_OPEN = "{getItem(){return '[\"G1\",\"G2\",\"G3\",\"G4\"]'},setItem(){}}"

    def publishers_built(self, *pubs):
        s = self.sal  # 配属先は A だけ。採用元が複数（親会社の募集で子会社に配属される枠など）
        return self.built(*[self.case(p, s(p + "枠", 300000 + 10000 * i)) for i, p in enumerate(pubs)])

    def run_js(self, built, body, storage="{getItem(){return null},setItem(){}}"):
        html = bv.build_html(*built)
        payload = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)[1]
        region = html[html.index("// --- 会社ごとの収納・展開 ---"):html.index("// --- 会社ごとの収納・展開ここまで ---")]
        cut = lambda text, start, end: text.split(start, 1)[1].split(end, 1)[0]
        pieces = {
            "__PAYLOAD__": payload, "__STORAGE__": storage, "__REGION__": region, "__BODY__": body,
            "__TEXT__": re.search(r"const text=r=>[^\n]+", html)[0],
            "__MK__": re.search(r"const MK=[^\n]+", html)[0], "__SHT__": re.search(r"const SHT=[^\n]+", html)[0],
            "__VNODE__": re.search(r"function vnode\(c\)\{[^\n]+\}", html)[0],
            "__SELECT__": "function selectRow(tr){" + cut(html, "function selectRow(tr){", "const hd="),
            "__SORT__": "function key(c,i){" + cut(html, "function key(c,i){", "// --- 〃"),
            "__RENDER__": "function render(){" + cut(html, "function render(){", "function buildRow"),
            "__BUILD__": "function buildRow(r){" + cut(html, "function buildRow(r){", "// --- 会社ごとの収納・展開 ---"),
            "__CELL__": "function cellDetail" + cut(html, "function cellDetail", "function safeUrl"),
            "__POPUP__": "const SOURCE_WORDS=" + cut(html, "const SOURCE_WORDS=", "function renderNotes"),
        }
        script = r"""
const D=__PAYLOAD__;
const NC=D.columns.length,CI=n=>D.columns.indexOf(n),unitOf=i=>{const m=/（(.+)）$/.exec(D.full[i]);return m?m[1]:''};
let view={previous:true,unofficial:true,shift:false,degree:'学士'},shiftOn=false;
const sv=c=>shiftOn&&c[5]?c[5]:null,txt=c=>(sv(c)||c)[0],numeric=new Set(D.numeric),startCols=new Set();
__TEXT__
D.rows.forEach(r=>r.hay=text(r));
class N{constructor(t){this.tag=t;this.children=[];this.className='';this.attrs={};this.s=new Set();
 this.classList={add:c=>this.s.add(c),remove:c=>this.s.delete(c),contains:c=>this.s.has(c)}}
 append(...xs){this.children.push(...xs)}prepend(...xs){this.children.unshift(...xs)}appendChild(x){this.children.push(x);return x}
 setAttribute(k,v){this.attrs[k]=v}removeAttribute(){this.className=''}get cells(){return this.children}}
const document={createElement:t=>new N(t),createTextNode:s=>({text:s}),createDocumentFragment:()=>({rows:[],appendChild(tr){this.rows.push(tr)}}),
 getElementById:()=>({scrollTop:0,scrollIntoView(){}})};
function el(t,txt,cls){const e=document.createElement(t);if(txt!==undefined)e.textContent=txt;if(cls)e.className=cls;return e}
function tg(txt,title){const t=el('button',txt,'tg');t.title=title;return t}
__MK__
__SHT__
__VNODE__
const rowCache=new Map(),info={},tb={replaceChildren(f){this.rows=f.rows}};
let selected=null,selCol=-1,selTd=null,selTr=null,sortCol=-1,sortDir=1,query='',popups=[],panels=0;
function closePop(){}function applyFold(){}function renderPanel(){panels++}function narrow(){return false}
let hoverTr=null;function showHover(){} // ホバーの帯は DOM が要るので、ここでは行・選択だけを見る
function openPop(anchor,build){popups.push([anchor,build])}function dittoSrc(){return -1}
const localStorage=__STORAGE__;
__REGION__
__CELL__
__POPUP__
__SELECT__
__SORT__
__RENDER__
__BUILD__
const shown=()=>tb.rows.map(tr=>[...rowCache].find(([r,t])=>t===tr)[0]);
const label=r=>(r[NC].members?'会社:':'')+r[1][0];
const btn=tr=>tr.children[0].children.filter(c=>c.className==='ct');          // 配属先セルの開閉の表示（+N／−）
const clickCell=(i,col=0)=>{const tr=tb.rows[i],td=tr.children[col];td.cellIndex=col;tr.onclick({target:{closest:()=>td}})};
const c0=i=>{const td=tb.rows[i].children[0];return [td.rowSpan,!!td.hidden,btn(tb.rows[i]).map(b=>b.textContent)]};
__BODY__
"""
        for k in [k for k in pieces if k != "__BODY__"] + ["__BODY__"]:  # 本文は最後に置換する
            script = script.replace(k, pieces[k])
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    # --- 会社行のセル ---
    def test_cells_are_the_shared_value_or_not_equal_with_all_values_listed(self):
        out = self.run_js(self.main_built(), """
render();const rows=shown(),by=Object.fromEntries(rows.map(r=>[r[NC].p,r]));
const cell=(p,col)=>by[p][CI(col)];
const names=rows.filter(r=>r[NC].members).flatMap(r=>[0,1,NC-1].map(i=>r[i][0]));
console.log(JSON.stringify({labels:rows.map(label),
 P:{sal:cell('P','学士初任給'),assign:cell('P','配属先')[0],pub:cell('P','採用元')[0],emp:cell('P','従業員数')[0],slot:cell('P','応募枠')},
 U:{sal:cell('U','学士初任給')[0],deadline:cell('U','最初の締切')},T:cell('T','学士初任給'),
 noNotEqualInNames:!names.includes('≠'),
 rIsOriginalRow:rows.find(r=>r[NC].p==='R')===D.rows.find(r=>r[NC].p==='R')}))
""")
        self.assertEqual(out["labels"], ["会社:甲／乙／丙", "会社:Q甲／Q乙", "R甲", "会社:T甲／T乙", "会社:U甲／U乙"])
        self.assertEqual(out["P"]["sal"], ["≠", None, "", "30／25／35"])  # 全文欄に値の一覧
        self.assertEqual(out["P"]["assign"], "採用元社")  # 全メンバーが同じ値ならその値
        self.assertEqual(out["P"]["pub"], "採用元社")
        self.assertEqual(out["P"]["emp"], "100")
        self.assertEqual(out["T"][0], "≠")  # 値あり・なしが混ざれば ≠
        self.assertEqual(out["U"]["sal"], "—")  # 全部「記録なし」ならそのまま
        self.assertEqual(out["U"]["deadline"][0], "≠")
        self.assertEqual(out["P"]["slot"][0], "甲／乙／丙")
        self.assertTrue(out["noNotEqualInNames"])  # 名前の列は ≠ にしない
        self.assertTrue(out["rIsOriginalRow"])  # 1行の配属先は元の行のまま

    def test_same_display_keeps_marks_and_any_visible_difference_is_not_equal(self):
        out = self.run_js(self.main_built(), """
const f=(...cs)=>sameCell(cs);
console.log(JSON.stringify([
 f(['30.0',30,'前'],['30.0',30,'非']),            // 表示が同じなら値。印は和集合（前非+ の順）
 f(['30.0',30,'非+'],['30.0',30,'前']),
 f(['30.0',30,''],['30.0',30,'','基本給のみ']),    // 全文が違う
 f(['30.0',30,'',' ','学'],['30.0',30,'','','修']), // 前置（学位）が違う
 f(['4.1',4.1,'','4.1万円/20時間'],['4.1',4.1,'','4.1万円/30時間']),
 f(['—',null,''],['—',null,'']),
 f(['26/3/1',1,'',' ',' ',['27/3/1','2027']],['26/3/1',1,''])  // ずらした表示かどうか
].map(c=>c.slice(0,4))))
""")
        self.assertEqual(out[0], ["30.0", 30, "前非"])
        self.assertEqual(out[1], ["30.0", 30, "前非+"])
        self.assertEqual(out[2][0], "≠")
        self.assertEqual(out[3][0], "≠")
        self.assertEqual(out[4], ["≠", None, "", "4.1（4.1万円/20時間）／4.1（4.1万円/30時間）"])  # 全文が違うので値の一覧にも全文を添える
        self.assertEqual(out[5][0], "—")

    def test_name_columns_are_joined_with_the_slot_rule_and_never_not_equal(self):
        for pubs, expected_label, expected_tip in (
                (("P", "Q", "S"), "採用元社／採用元Q／採用元S", ["採用元社", "採用元Q", "採用元S（株式会社採用元S）"]),
                (("P", "Q", "S", "R"), "採用元社 ほか3社", ["採用元社", "採用元Q", "採用元S（株式会社採用元S）", "採用元R"])):
            out = self.run_js(self.publishers_built(*pubs), """
render();const cr=shown()[0],c=cr[NC-1],a=cr[0],s=cr[1];
console.log(JSON.stringify({n:shown().length,label:label(cr),pub:c,assign:a,slot:s[0],title:cr[NC].title,pubLine:cr[NC].pub,sal:cr[CI('学士初任給')][0]}))
""")
            self.assertEqual(out["n"], 1)
            self.assertEqual(out["pub"][0], expected_label)  # 採用元は違うので、枠と同じ規則で名前を並べる
            self.assertEqual(out["pub"][3].split("\n"), expected_tip)  # ツールチップには全部の名前
            self.assertEqual(out["pub"][1], "採用元社")
            self.assertEqual(out["assign"][0], "配属A")  # 配属先は常に同じ
            self.assertEqual(out["title"][0], "配属A")
            full = {"P": "採用元社", "Q": "採用元Q", "S": "株式会社採用元S", "R": "採用元R"}
            self.assertEqual(out["pubLine"], "、".join(full[p] for p in pubs))  # 右パネルの採用元は全部の正式名
            self.assertEqual(out["sal"], "≠")
        self.assertEqual(out["slot"], "P枠 ほか3枠")

    def test_name_list_rule_is_shared_between_python_and_js_for_every_unit(self):
        # 統合行（Python）・応募枠セル・採用元と配属先の連結（JS）が、同じ規則・同じ定数を使う
        for unit in ("枠", "社"):
            for n in (1, 2, 3, 4, 6):
                items = [[f"短{i}", f"短{i}" if i % 2 else f"正式{i}"] for i in range(n)]
                out = self.run_js(self.main_built(), "console.log(JSON.stringify(nameList(%s,%s,'k','前')))"
                                  % (json.dumps(items, ensure_ascii=False), json.dumps(unit, ensure_ascii=False)))
                self.assertEqual(out, bv.name_list_cell([tuple(x) for x in items], unit, "k", "前"), (unit, n))
        self.assertEqual(bv.name_list_cell([("a", "a"), ("b", "B")] * 2, "社", 1)[0], "a ほか3社")
        self.assertEqual(bv.name_list_cell([("a", "A")] * 4, "枠", 1)[0], "a ほか3枠")
        self.assertEqual(bv.NAME_LIST, {"sep": "／", "limit": 3, "more": "{first} ほか{n}{unit}"})
        for n in (2, 3, 4, 6):  # 統合行そのもの（slot セルの連結）とも一致する
            names = [f"正式{i}" for i in range(n)]
            shorts = {nm: f"短{i}" for i, nm in enumerate(names)}
            case = self.case("P", *[slot(2028, ["A"], name=nm) for nm in names])
            with mock.patch.object(bv, "load_slot_short_names", return_value={"P_2028": shorts}):
                built = bv.build_rows(self.PROFILES, [case])
            self.assertEqual(len(built[0]), 1)
            row = built[0][0]
            out = self.run_js(built, "console.log(JSON.stringify(slotCell(%s,%s)))" % (
                json.dumps(row[-1]["slots"], ensure_ascii=False), json.dumps(row[1][1])))
            self.assertEqual(out, row[1], n)

    def test_slot_cell_deduplicates_names_that_repeat_across_members(self):
        # 同じ応募枠名が採用元違いで複数のメンバーにあっても、連結名・「ほかN枠」は枠名の重複を数えない
        built = self.built(*[self.case(p, *[slot(2028, ["A"], [(300000 + 10000 * i, "official")], name=f"枠{i}") for i in range(5)])
                             for p in "PQ"])
        out = self.run_js(built, "render();const c=shown()[0];console.log(JSON.stringify({cell:c[1],rows:D.rows.length}))")
        self.assertEqual(out["rows"], 10)  # 同じ5枠を2社が募集し、どちらも配属先は A
        self.assertEqual(out["cell"][0], "枠0 ほか4枠")
        self.assertEqual(out["cell"][2], "")
        self.assertEqual(out["cell"][3].split("\n"), [f"枠{i}" for i in range(5)])

    # --- 収納・展開、行 ---
    def test_collapsed_by_default_with_the_state_in_the_assignment_cell_and_no_toggle_for_a_single_row(self):
        out = self.run_js(self.main_built(), """
render();const trs=tb.rows,rows=shown();
const attrs=i=>{const td=trs[i].children[0];return [td.tabIndex,td.attrs['aria-expanded'],td.title,td.className.includes(' gtog')]};
console.log(JSON.stringify({n:trs.length,matched:info.textContent.split('　')[0],
 toggles:rows.map((r,i)=>[label(r),btn(trs[i]).map(b=>b.textContent)]),keys:groupKeys,
 notEqualMuted:trs[0].children[CI('学士初任給')].className.includes(' na'),
 plain:trs.every(tr=>tr.children.every(td=>!td.className.includes(' ne')&&!td.className.includes(' co'))),
 slots:trs.map(tr=>tr.children[0].children[0].className),oneCell:trs.every(tr=>tr.children[0].rowSpan===1&&!tr.children[0].hidden),
 company:attrs(0),single:attrs(2),
 pubCellHasNoButton:trs.every(tr=>tr.children[NC-1].children.every(c=>c.tag!=='button'))}))
""")
        self.assertEqual(out["n"], 5)
        self.assertEqual(out["matched"], "10 / 10 行（表示 5 行）")
        self.assertEqual(out["toggles"], [["会社:甲／乙／丙", ["+3"]], ["会社:Q甲／Q乙", ["+2"]], ["R甲", []],  # +N は会社行にまとめているメンバー行の数
                                          ["会社:T甲／T乙", ["+2"]], ["会社:U甲／U乙", ["+2"]]])
        self.assertEqual(out["keys"], ["P", "Q", "T", "U"])
        self.assertEqual(out["slots"], ["ct", "ct", "ct0", "ct", "ct"])  # 開閉できない行（1行だけの配属先）も同じ幅の空きを置く
        self.assertTrue(out["oneCell"])  # 収納時の会社行と1行だけの配属先は1行1セル
        # 開閉できるセルは、状態を title と aria-expanded に持ち、キーボードで届く。1行だけの配属先のセルは今までどおり
        self.assertEqual(out["company"][:2], [0, "false"])
        self.assertTrue(out["company"][2].startswith("この配属先の3行を展開\n"))
        self.assertTrue(out["company"][3])
        self.assertEqual(out["single"], [None, None, "採用元R", False])
        self.assertTrue(out["notEqualMuted"])  # 「≠」は「—」と同じ控えめな色
        self.assertTrue(out["plain"])  # 会社行だけの背景・「≠」専用のスタイルはない
        self.assertTrue(out["pubCellHasNoButton"])
    def test_clicking_the_cell_expands_and_collapses_and_selects_the_row(self):
        out = self.run_js(self.main_built(), """
render();const before=label(selected);
const td=i=>tb.rows[i].children[0],attr=i=>[td(i).attrs['aria-expanded'],td(i).title.split('\\n')[0]];
const collapsedAttr=attr(1);
clickCell(1);                                      // Q の会社行のセルをクリック：展開し、最初のメンバー行を選ぶ
const open={labels:shown().map(label),glyphs:tb.rows.map(tr=>btn(tr).map(b=>b.textContent)),popups:popups.length,selected:label(selected),
 attr:attr(1),panels,sel:tb.rows.findIndex(tr=>tr.className==='sel')};
clickCell(1);                                      // 結合したセルをクリック：収納し、会社行を選ぶ
const closed={labels:shown().map(label),glyphs:tb.rows.map(tr=>btn(tr).map(b=>b.textContent)),selected:label(selected),
 sel:tb.rows.findIndex(tr=>tr.className==='sel')};
clickCell(0,CI('学士初任給'));                      // 開閉のセル以外は今までどおり（行の選択とポップアップ）
console.log(JSON.stringify({before,collapsedAttr,open,closed,clickPopups:popups.length,clickSelected:label(selected),selCol:selCol===CI('学士初任給')}))
""")
        self.assertEqual(out["collapsedAttr"], ["false", "この配属先の2行を展開"])
        self.assertEqual(out["open"]["labels"], ["会社:甲／乙／丙", "Q甲", "Q乙", "R甲", "会社:T甲／T乙", "会社:U甲／U乙"])  # 会社行は出ない
        self.assertEqual(out["open"]["glyphs"], [["+3"], ["−"], [], [], ["+2"], ["+2"]])  # 「−」は結合したセルに1つだけ
        self.assertEqual(out["open"]["attr"], ["true", "この配属先の枠を会社行に収納"])  # 記号・title・aria-expanded は状態に合わせる
        self.assertEqual(out["open"]["popups"], 0)  # 開閉ではポップアップを開かない
        self.assertEqual(out["open"]["selected"], "Q甲")  # 開閉と同時に行を選ぶ（右パネルも更新される）
        self.assertEqual(out["open"]["sel"], 1)
        self.assertEqual(out["closed"]["labels"], ["会社:甲／乙／丙", "会社:Q甲／Q乙", "R甲", "会社:T甲／T乙", "会社:U甲／U乙"])
        self.assertEqual(out["closed"]["glyphs"], [["+3"], ["+2"], [], ["+2"], ["+2"]])
        self.assertEqual((out["closed"]["selected"], out["closed"]["sel"]), ("会社:Q甲／Q乙", 1))
        self.assertEqual(out["clickPopups"], 1)
        self.assertEqual(out["clickSelected"], "会社:甲／乙／丙")
        self.assertTrue(out["selCol"])

    def test_single_row_assignment_cell_does_not_toggle_and_keyboard_toggles_the_others(self):
        out = self.run_js(self.main_built(), """
render();const n=shown().length;
clickCell(2);                                         // R甲（1行だけの配属先）のセル：今までの行クリック
const single={n:shown().length,selected:label(selected),writes:localStorage.writes,selCol,tab:tb.rows[2].children[0].tabIndex??null,key:tb.rows[2].children[0].onkeydown};
const td=tb.rows[0].children[0],log=[];const press=k=>td.onkeydown({key:k,preventDefault(){log.push(k)}});
press('a');const afterOther=shown().length;press('Enter');const afterEnter=shown().length;
const td2=tb.rows[0].children[0];td2.onkeydown({key:' ',preventDefault(){log.push('space')}});
console.log(JSON.stringify({n,single:{...single,key:typeof single.key},afterOther,afterEnter,afterSpace:shown().length,log,tab:td.tabIndex,selected:label(selected)}))
""", "{writes:0,getItem(){return null},setItem(){this.writes++}}")
        self.assertEqual(out["single"], {"n": 5, "selected": "R甲", "writes": 0, "selCol": 0, "tab": None, "key": "undefined"})
        self.assertEqual((out["n"], out["afterOther"], out["afterEnter"], out["afterSpace"]), (5, 5, 7, 5))  # Enter で展開、Space で収納
        self.assertEqual(out["log"], ["Enter", "space"])  # 開閉に使うキーだけ既定動作を止める
        self.assertEqual(out["tab"], 0)
    def test_only_adjacent_rows_of_the_same_assignment_are_merged_after_sorting(self):
        out = self.run_js(self.main_built(), """
const spans=()=>shown().map((r,i)=>[label(r),...c0(i)]);
const col=CI('学士初任給');
render();const natural=spans();
sortCol=col;sortDir=1;render();const asc=spans();
sortDir=-1;render();const desc=spans();
sortCol=-1;render();const cleared=spans();
console.log(JSON.stringify({natural,asc,desc,cleared,cells:tb.rows.every(tr=>tr.children.length===NC)}))
""", self.ALL_OPEN)
        T, F = True, False
        natural = [["甲", 3, F, ["−"]], ["乙", 1, T, []], ["丙", 1, T, []], ["Q甲", 2, F, ["−"]], ["Q乙", 1, T, []],
                   ["R甲", 1, F, []],  # 1行だけの配属先は1行1セルで記号なし
                   ["T甲", 2, F, ["−"]], ["T乙", 1, T, []], ["U甲", 2, F, ["−"]], ["U乙", 1, T, []]]
        self.assertEqual(out["natural"], natural)
        # 並べ替えは表に出ている行そのもの。隣り合った同じ配属先だけ結合し、離れた行はそれぞれ別のセル（それぞれに「−」）
        self.assertEqual(out["asc"], [
            ["T乙", 1, F, ["−"]], ["乙", 1, F, ["−"]], ["R甲", 1, F, []], ["Q甲", 1, F, ["−"]], ["甲", 2, F, ["−"]], ["丙", 1, T, []],
            ["Q乙", 1, F, ["−"]], ["T甲", 1, F, ["−"]], ["U甲", 2, F, ["−"]], ["U乙", 1, T, []]])  # 値のない行は末尾（元の順）
        self.assertEqual(out["desc"], [
            ["Q乙", 1, F, ["−"]], ["丙", 2, F, ["−"]], ["甲", 1, T, []], ["Q甲", 1, F, ["−"]], ["R甲", 1, F, []], ["乙", 1, F, ["−"]],
            ["T乙", 2, F, ["−"]], ["T甲", 1, T, []], ["U甲", 2, F, ["−"]], ["U乙", 1, T, []]])
        self.assertEqual(out["cleared"], natural)  # 並べ替えを解除すると元の順に戻り、また結合される
        self.assertTrue(out["cells"])  # セルの数（列番号）は変えない。結合は rowSpan と非表示で行う
    def test_cached_rows_get_their_merge_reset_when_neighbours_change(self):
        out = self.run_js(self.filter_built(), """
render();clickCell(0);
const open=[0,1,2].map(c0),nodes=tb.rows.slice();
// 乙だけに絞ると、隠れていた乙のセルが1行1セルに戻る（記号が同じ「なし」なのでノードは作り直さずに使い回す）
query='乙コース';render();const single=[c0(0),tb.rows[0]===nodes[1]];
// 戻すと、乙は再び隠れ、先頭の甲が結合する
query='';render();const back=[0,1,2].map(c0);
// 丙が先頭に来る並べ替え：丙のノード（結合なし）が結合の先頭になり、甲のノード（元は先頭）は隠れる
sortCol=CI('学士初任給');sortDir=-1;render();const desc=[0,1,2].map(c0);const order=shown().map(label);
sortDir=1;render();const asc=[0,1,2].map(c0);
// 配属先を収納して会社行だけにする
clickCell(0);const collapsed=[c0(0),shown().length];
console.log(JSON.stringify({open,single,back,desc,order,asc,collapsed}))
""")
        T, F = True, False
        self.assertEqual(out["open"], [[3, F, ["−"]], [1, T, []], [1, T, []]])
        self.assertEqual(out["single"], [[1, F, []], True])
        self.assertEqual(out["back"], [[3, F, ["−"]], [1, T, []], [1, T, []]])
        self.assertEqual(out["order"], ["丙別枠", "甲コース", "乙コース"])
        self.assertEqual(out["desc"], [[3, F, ["−"]], [1, T, []], [1, T, []]])
        self.assertEqual(out["asc"], [[3, F, ["−"]], [1, T, []], [1, T, []]])
        self.assertEqual(out["collapsed"], [[1, F, ["+3"]], 1])

    def test_cache_survives_sorting_that_splits_and_rejoins_an_assignment(self):
        out = self.run_js(self.values_built(), """
const g4=()=>shown().map((r,i)=>[label(r),...c0(i)]).filter(x=>x[0].startsWith('G4'));
render();const natural=g4(),nodes=new Map(shown().map((r,i)=>[label(r),tb.rows[i]]));
sortCol=CI('学士初任給');sortDir=1;render();const split=g4();                // G4b(10) と G4a(30) が離れる
sortCol=-1;render();const joined=g4();                                      // 元の順に戻ると隣り合って結合される
const reusedA=tb.rows[shown().findIndex(r=>label(r)==='G4a')]===nodes.get('G4a');   // 「−」の持ち主は同じ行（G4a）なのでノードを使い回す
console.log(JSON.stringify({natural,split,joined,reusedA}))
""", self.VALUES_OPEN)
        T, F = True, False
        self.assertEqual(out["natural"], [["G4a", 2, F, ["−"]], ["G4b", 1, T, []]])
        self.assertEqual(out["split"], [["G4b", 1, F, ["−"]], ["G4a", 1, F, ["−"]]])  # 離れた同じ配属先は別のセル。どちらにも「−」
        self.assertEqual(out["joined"], out["natural"])
        self.assertTrue(out["reusedA"])
    def test_assignment_name_starts_at_the_same_place_in_every_row_and_the_cell_looks_clickable(self):
        html = bv.build_html(*self.main_built())
        self.assertIn("td.c0[rowspan]:not([rowspan=\"1\"]){vertical-align:top}", html)
        self.assertIn(".ct,.ct0{", html)
        self.assertIn("--ct-w:36px", html)
        self.assertIn(".c0{left:0;min-width:calc(var(--name-w) + var(--ct-w))", html)  # 固定表示（sticky）の左端の列。表示の分だけ広げる
        self.assertIn(".c1{left:calc(var(--name-w) + var(--ct-w))", html)
        self.assertIn("const hint=i===0?groupSlot(td,r,tr.glyph):''", html)
        # 開閉できるセルは押せると分かる見た目（ポインター・控えめなホバー・フォーカス）。選択行の緑とは別の青系
        self.assertIn("td.gtog{cursor:pointer}", html)
        self.assertIn("td.gtog:hover{background:color-mix(in srgb,var(--link) 9%,var(--surface))}", html)
        self.assertIn("td.gtog:focus-visible{outline:2px solid var(--link)", html)
        self.assertNotIn("button class=\"ct\"", html)
        # 開閉できるセルのクラス名は、ほかの部品と共有しない（以前 .tgl を使い、ツールバーのスイッチ用の inline-flex と gap が配属先セルに掛かって社名の位置がずれた）
        self.assertEqual(html.count(".gtog"), html.count("td.gtog"))
        self.assertNotIn("td.tgl", html)
        out = self.run_js(self.main_built(), """
render();const kinds=tb.rows.map(tr=>tr.children[0].children[0].className);   // 会社行(ct)・結合セルの先頭(ct)・隠れる行(ct0)・1行だけ(ct0)
console.log(JSON.stringify({labels:shown().map(label),kinds,cls:tb.rows.map(tr=>tr.children[0].className.includes(' gtog'))}))
""", '{getItem(){return \'["P"]\'},setItem(){}}')
        self.assertEqual(out["labels"], ["甲", "乙", "丙", "会社:Q甲／Q乙", "R甲", "会社:T甲／T乙", "会社:U甲／U乙"])
        self.assertEqual(out["kinds"], ["ct", "ct0", "ct0", "ct", "ct0", "ct", "ct"])
        self.assertEqual(out["cls"], [True, False, False, True, False, True, True])  # 結合した先頭のセルと会社行だけが開閉できる
    def test_selection_follows_the_toggle(self):
        out = self.run_js(self.filter_built(), """
render();const cr=shown()[0];selected=cr;
clickCell(0);                                                   // 展開：最初のメンバー行を選ぶ
const expanded=[label(selected),shown().length];
selected=shown()[2];                                           // 別のメンバーを選んでいても
clickCell(0);                                                   // 収納（結合したセル）：会社行を選ぶ
const collapsed=[label(selected),shown().length,selected===cr];
selected=cr;allToggle().onclick({stopPropagation(){}});const all=label(selected);   // 見出しの全開閉でも、会社行は最初のメンバー行へ
selected=shown()[1];const keep=label(selected);query='甲コース';render();                    // 絞り込みで会社行がなくなる
console.log(JSON.stringify({expanded,collapsed,all,keep,single:label(selected)}))
""")
        self.assertEqual(out["expanded"], ["甲コース", 3])
        self.assertEqual(out["collapsed"], ["会社:甲コース／乙コース／丙別枠", 1, True])
        self.assertEqual(out["all"], "甲コース")
        self.assertEqual(out["keep"], "乙コース")
        self.assertEqual(out["single"], "甲コース")  # 絞り込みで選んでいた行が消えたら、先頭の行へ
        # 別の行を選んでいても、クリックした配属先の行へ移る。並べ替え中は、並べ替え後の表で最初に出るメンバー行
        out = self.run_js(self.values_built(), """
sortCol=CI('学士初任給');sortDir=1;render();
const before=shown().map(label);                               // 昇順: G5a(15) G2 G3 G1 のあと、値なしの G4（≠）
selected=shown()[0];clickCell(4);                              // G4 の会社行をクリック
const expanded=[label(selected),shown().map(label)];
const last=shown().length-1;clickCell(last);                   // G4a のセル（「−」）をクリック
const collapsed=[label(selected),shown().length];
console.log(JSON.stringify({before,expanded,collapsed}))
""")
        self.assertEqual(out["before"], ["G5a", "会社:G2a／G2b", "会社:G3a／G3b", "会社:G1a／G1b", "会社:G4a／G4b"])
        self.assertEqual(out["expanded"], ["G4b", ["G4b", "G5a", "会社:G2a／G2b", "会社:G3a／G3b", "会社:G1a／G1b", "G4a"]])
        self.assertEqual(out["collapsed"], ["会社:G4a／G4b", 5])
    def test_collapsed_company_rows_sort_as_one_row_by_their_own_value_and_not_equal_goes_last(self):
        out = self.run_js(self.values_built(), """
const seq=()=>shown().map(label),col=CI('学士初任給');
render();const natural=seq();const vals=shown().map(r=>r[col][0]);
sortCol=col;sortDir=1;render();const asc=seq();
sortDir=-1;render();const desc=seq();
const keys=[key(['≠',null,'','x'],col),key(['—',null,''],col),key(['30',300000,''],col)];
sortCol=-1;render();const cleared=seq();
const cr=companyRows.get('G1');sortCol=col;render();const stable=companyRows.get('G1')===cr;
console.log(JSON.stringify({natural,vals,asc,desc,cleared,keys,stable}))
""")
        self.assertEqual(out["natural"], ["会社:G1a／G1b", "会社:G2a／G2b", "会社:G3a／G3b", "会社:G4a／G4b", "G5a"])
        self.assertEqual(out["vals"], ["30", "20", "25", "≠", "15"])
        # 会社行は1行として、自分のセルの値（全部同じならその値）で並ぶ。「≠」は昇順でも降順でも末尾
        self.assertEqual(out["asc"], ["G5a", "会社:G2a／G2b", "会社:G3a／G3b", "会社:G1a／G1b", "会社:G4a／G4b"])
        self.assertEqual(out["desc"], ["会社:G1a／G1b", "会社:G3a／G3b", "会社:G2a／G2b", "G5a", "会社:G4a／G4b"])
        self.assertEqual(out["keys"], [None, None, 300000])  # 「≠」の並べ替えキーは「—」と同じ null（常に末尾）
        self.assertEqual(out["cleared"], out["natural"])  # 解除すると元の順
        self.assertTrue(out["stable"])  # 並べ替えても会社行は作り直さない（行キャッシュを生かす）
        # 「≠」と「—」は同じ値なし。元の順のまま末尾にそろう
        out = self.run_js(self.main_built(), """
const col=CI('学士初任給');sortCol=col;sortDir=1;render();const asc=shown().map(label);sortDir=-1;render();
console.log(JSON.stringify({asc,desc:shown().map(label)}))
""")
        self.assertEqual(out["asc"], ["R甲", "会社:甲／乙／丙", "会社:Q甲／Q乙", "会社:T甲／T乙", "会社:U甲／U乙"])
        self.assertEqual(out["desc"], out["asc"])

    def test_expanded_member_rows_sort_independently_and_clearing_the_sort_restores_the_original_order(self):
        out = self.run_js(self.values_built(), """
const seq=()=>shown().map(label),col=CI('学士初任給');
render();const natural=seq();
sortCol=col;sortDir=1;render();const asc=seq();
sortDir=-1;render();const desc=seq();
sortCol=-1;render();
console.log(JSON.stringify({natural,asc,desc,cleared:seq()}))
""", self.VALUES_OPEN)
        # 会社ごとに連続させない。月給の同じ行は元の順のまま（安定）
        self.assertEqual(out["natural"], ["G1a", "G1b", "G2a", "G2b", "G3a", "G3b", "G4a", "G4b", "G5a"])
        self.assertEqual(out["asc"], ["G4b", "G5a", "G2a", "G2b", "G3a", "G3b", "G1a", "G1b", "G4a"])
        self.assertEqual(out["desc"], ["G1a", "G1b", "G4a", "G3a", "G3b", "G2a", "G2b", "G5a", "G4b"])
        self.assertEqual(out["cleared"], out["natural"])
    # --- 絞り込み ---
    def test_company_row_is_built_from_the_matching_members_only(self):
        out = self.run_js(self.filter_built(), """
const col=CI('学士初任給'),dl=CI('最初の締切');
render();const all=shown()[0];
query='コース';render();const two=shown().map(r=>[label(r),r[col][0],r[dl][0],r[NC].members.map(m=>m[1][0])]);
const twoTitle=shown()[0][NC].title,twoPub=shown()[0][NC].pub;
query='甲コース';render();const one=shown().map(r=>[label(r),!!r[NC].members,btn(tb.rows[0]).length]);const keys=groupKeys.length;
query='存在しない';render();const none=shown().length;
console.log(JSON.stringify({all:[all[col][0],all[CI('応募枠')][0]],two,twoTitle,twoPub,one,keys,none,info:info.textContent.split('　')[0]}))
""")
        self.assertEqual(out["all"], ["≠", "甲コース／乙コース／丙別枠"])
        self.assertEqual(out["two"], [["会社:甲コース／乙コース", "30", "≠", ["甲コース", "乙コース"]]])  # 合うメンバーだけなら月給は同じ
        self.assertEqual(out["twoTitle"], ["配属A", "甲コース", "乙コース"])
        self.assertEqual(out["twoPub"], "採用元F")
        self.assertEqual(out["one"], [["甲コース", False, 0]])  # 合うのが1行なら1行の配属先と同じ（会社行・ボタンなし）
        self.assertEqual(out["keys"], 0)
        self.assertEqual(out["none"], 0)
        self.assertEqual(out["info"], "0 / 3 行")

    # --- ポップアップ ---
    def test_not_equal_cell_popup_lists_blocks_per_slot_and_merges_identical_ones(self):
        out = self.run_js(self.filter_built(), """
render();const cr=shown()[0];
const brief=bs=>bs.map(b=>({heading:b.heading,shown:b.shown,extra:b.extra,name:b.name,values:b.records.map(x=>x.value)}));
console.log(JSON.stringify({salary:brief(metricBlocks(cr,CI('学士初任給'))),deadline:brief(metricBlocks(cr,CI('最初の締切'))),
 slot:metricBlocks(cr,CI('応募枠')).map(b=>[b.name,b.shown,b.target,b.heading,b.merged]),
 detail:{salary:!!cellDetail(cr,CI('学士初任給')),assign:cellDetail(cr,0),pub:cellDetail(cr,NC-1)},
 ctx:contextOf(cr)}))
""")
        salary = out["salary"]
        self.assertEqual([b["heading"] for b in salary], ["応募枠：甲コース（28卒）、乙コース（28卒）", "応募枠：丙別枠（28卒）"])
        self.assertEqual([b["shown"] for b in salary], ["30 万円/月", "35 万円/月"])  # 値が違う枠は別ブロックで一覧できる
        self.assertEqual([b["name"] for b in salary], ["初任給（学士）"] * 2)
        deadline = out["deadline"]
        self.assertEqual([b["heading"] for b in deadline], ["応募枠：甲コース（28卒）", "応募枠：乙コース（28卒）", "応募枠：丙別枠（28卒）"])
        self.assertEqual([b["shown"] for b in deadline], ["28/4/1", "28/5/1", "—（記録なし）"])
        # 応募枠セルは統合行と同じブロック（枠ごと・「応募枠：<枠名>（年度）」）
        self.assertEqual(out["slot"], [["応募枠", "甲コース", "28卒", True, True], ["応募枠", "乙コース", "28卒", True, True],
                                       ["応募枠", "丙別枠", "28卒", True, True]])
        self.assertTrue(out["detail"]["salary"])
        self.assertIsNone(out["detail"]["assign"])
        self.assertIsNone(out["detail"]["pub"])
        self.assertEqual(out["ctx"], "配属A ・ 甲コース／乙コース／丙別枠")

    def test_same_value_with_different_evidence_keeps_a_block_per_evidence(self):
        s = self.sal
        built = self.built(self.case("G", s("甲", 300000, url="https://a.example"), s("乙", 300000, url="https://b.example")))
        out = self.run_js(built, """
render();const cr=shown()[0],bs=metricBlocks(cr,CI('学士初任給'));
console.log(JSON.stringify({cell:cr[CI('学士初任給')][0],blocks:bs.map(b=>[b.heading,b.shown,b.records.map(r=>r.url)])}))
""")
        self.assertEqual(out["cell"], "30")  # 表示は同じなので値を出す
        self.assertEqual(out["blocks"], [["応募枠：甲（28卒）", "30 万円/月", ["https://a.example"]],
                                         ["応募枠：乙（28卒）", "30 万円/月", ["https://b.example"]]])

    def test_selection_blocks_keep_their_own_headings_and_open_ones_come_first(self):
        out = self.run_js(self.filter_built(), """
render();const cr=shown()[0],bs=metricBlocks(cr,CI('選考'));
console.log(JSON.stringify(bs.map(b=>[b.heading,!!b.collapsed])))
""")
        self.assertTrue(all(h.endswith("応募枠：甲コース（28卒）、乙コース（28卒）、丙別枠（28卒）") for h, _ in out))
        self.assertEqual(len(out), 11)  # 工程 + 10種。中身が同じなのでメンバーごとには増えない
        self.assertTrue(out[0][0].startswith("選考工程：—（記録なし）　応募枠："))

    # --- 保存 ---
    def test_state_is_saved_loaded_and_survives_unavailable_storage(self):
        out = self.run_js(self.main_built(), """
render();const before=shown().length;
clickCell(0);
const afterToggle=shown().length,ls=localStorage.last;
allToggle().onclick({stopPropagation(){}});
console.log(JSON.stringify({before,afterToggle,ls,all:shown().length,keys:localStorage.last}))
""", "{last:null,getItem(){return null},setItem(k,v){this.last=[k,JSON.parse(v)]}}")
        self.assertEqual((out["before"], out["afterToggle"]), (5, 7))  # 既定は全社収納。P を開くと会社行が3行に置き換わる
        self.assertEqual(out["ls"], ["viewerAssignOpen", ["P"]])
        self.assertEqual(out["all"], 10)  # 全部開くと会社行は出ず、メンバー行だけ
        self.assertEqual(out["keys"], ["viewerAssignOpen", ["P", "Q", "T", "U"]])
        # 保存済みの状態を読む（形が違う・壊れた値は無視して全社収納）。古い採用元単位のキーは読まない
        for stored, expected in (("'[\"Q\"]'", 6), ("'{\"x\":1}'", 5), ("'not json'", 5), ("'[1,2]'", 5)):
            out = self.run_js(self.main_built(), "render();console.log(JSON.stringify(shown().length))",
                              "{getItem(k){return k==='viewerAssignOpen'?%s:null},setItem(){}}" % stored)
            self.assertEqual(out, expected, stored)
        out = self.run_js(self.main_built(), "render();console.log(JSON.stringify(shown().length))",
                          "{getItem(k){return k==='viewerCompanyOpen'?'[\"P\",\"Q\"]':null},setItem(){}}")
        self.assertEqual(out, 5)
        # localStorage が使えない（読み書きが例外）ときも描画でき、開閉はそのページの間は効く
        broken = "{getItem(){throw new Error('denied')},setItem(){throw new Error('denied')}}"
        out = self.run_js(self.main_built(), """
render();const before=shown().length;
clickCell(0);
console.log(JSON.stringify([before,shown().length]))
""", broken)
        self.assertEqual(out, [5, 7])
        out = self.run_js(self.main_built(), "console.log(JSON.stringify(typeof localStorage))", "undefined")
        self.assertEqual(out, "undefined")

    # --- 見出しの全開閉ボタン ---
    def test_header_button_is_in_the_assignment_column_heading_and_shows_the_state(self):
        html = bv.build_html(*self.main_built())
        self.assertIn("if(D.columns[i]==='配属先')th.append(allToggle());", html)
        self.assertNotIn("D.columns[i]==='採用元')th.append(", html)  # 採用元の見出しにはボタンを置かない
        toolbar = re.search(r'<div class="tools">(.*?)</header>', html, re.S)[1]
        self.assertNotIn("すべて展開", toolbar)
        self.assertNotIn("すべて収納", toolbar)
        out = self.run_js(self.main_built(), """
render();const stops=[];const states=[];
const snap=()=>{const b=allToggle();states.push([b.textContent,b.attrs['aria-expanded'],!!b.disabled,b.title])};
snap();allToggle().onclick({stopPropagation(){stops.push(1)}});snap();const open=shown().length;
allToggle().onclick({stopPropagation(){stops.push(1)}});snap();
query='存在しない';render();snap();
console.log(JSON.stringify({states,open,closed:(query='',render(),shown().length),stops:stops.length}))
""")
        self.assertEqual(out["states"][0][:3], ["すべて展開", "false", False])
        self.assertEqual(out["states"][1][:3], ["すべて収納", "true", False])  # 全部開いているときは「すべて収納」
        self.assertEqual(out["states"][2][:3], ["すべて展開", "false", False])
        self.assertEqual(out["states"][3][:3], ["すべて展開", "false", True])  # 対象の配属先がないとき
        self.assertEqual((out["open"], out["closed"], out["stops"]), (10, 5, 2))  # 見出しの並べ替えには伝えない

    def test_all_toggle_applies_to_the_assignments_in_view(self):
        out = self.run_js(self.filter_built(), """
render();allToggle().onclick({stopPropagation(){}});const a=shown().map(label);
query='コース';render();const b=allToggle().textContent;
console.log(JSON.stringify({a,b}))
""")
        self.assertEqual(out["a"], ["甲コース", "乙コース", "丙別枠"])
        self.assertEqual(out["b"], "すべて収納")

    def test_descriptions_and_legend_follow_the_new_behaviour(self):
        d = bv.column_descriptions()
        self.assertEqual(d["採用元"]["short"], "募集を出している企業（応募窓口）。")
        self.assertIn("+N", d["配属先"]["short"])
        self.assertIn("−", d["配属先"]["short"])
        self.assertNotIn("▸", d["配属先"]["short"])  # 列の折りたたみ（▸ ◂ ▾）とは別の記号
        html = bv.build_html(*self.main_built())
        self.assertIn("会社行で枠ごとに値が異なる", html)
        self.assertIn("配属先の見出しの「すべて展開／すべて収納」", html)
        self.assertIn("「+N」", html)
        self.assertIn("配属先のセルをクリックすると", html)
        self.assertIn("「≠」は「—」と同じ値なしとして、昇順でも降順でも末尾に並びます", html)  # 並べ替えでの扱い
        self.assertIn("同じ配属先のセルを1つに結合", html)
        self.assertNotIn("採用元の ▸ ▾", html)
        self.assertNotIn("各行の ▾", html)
        self.assertNotIn("採用元の見出しの「すべて展開", html)

    # --- 行キャッシュ ---
    def test_row_cache_reuses_nodes_and_drops_stale_nodes(self):
        out = self.run_js(self.filter_built(), """
render();const first=tb.rows[0],cr=shown()[0];
render();const reused=tb.rows[0]===first;
clickCell(0);                                                  // 展開
const members=shown(),nodes=tb.rows.slice(),glyphs=tb.rows.map(tr=>tr.glyph),sameRow=companyRows.get('A')===cr;
render();const reusedMembers=tb.rows.every((tr,i)=>tr===nodes[i]);
query='甲コース';render();const single=[tb.rows[0]!==nodes[0],tb.rows[0].glyph,btn(tb.rows[0]).length];   // 1行になると ▾ のないノードに作り直す
query='';render();const back=[tb.rows[0]!==nodes[0],tb.rows[0].glyph,btn(tb.rows[0]).length];
clickCell(0);                                                  // 収納
const cr1=shown()[0],collapsed=!!cr1[NC].members&&shown().length===1;   // 1行になって会社行が消えたあと、作り直した会社行
query='コース';render();
const cr2=shown()[0],replaced=cr2!==cr1,oldDropped=!rowCache.has(cr1),n2=cr2[NC].members.length;
console.log(JSON.stringify({reused,glyphs,sameRow,reusedMembers,single,back,collapsed,replaced,oldDropped,n2,companyRows:companyRows.size}))
""")
        self.assertTrue(out["reused"])
        self.assertEqual(out["glyphs"], ["−", "", ""])  # 「−」は先頭のメンバー行だけ
        self.assertTrue(out["sameRow"])  # 会社行のオブジェクトは同じ
        self.assertTrue(out["reusedMembers"])
        self.assertEqual(out["single"], [True, "", 0])
        self.assertEqual(out["back"], [True, "−", 1])
        self.assertTrue(out["collapsed"])
        self.assertTrue(out["replaced"])
        self.assertTrue(out["oldDropped"])  # 使わなくなった会社行のノードはキャッシュから捨てる
        self.assertEqual(out["n2"], 2)
        self.assertEqual(out["companyRows"], 1)

    def test_display_mode_change_resets_company_rows_and_cache(self):
        html = bv.build_html(*self.main_built())
        body = html.split("function displayRows", 1)[1].split("function setView", 1)[0]
        self.assertIn("rowCache.clear();companyRows=new Map();", body)

    # --- 表示 ---
    def test_not_equal_is_not_turned_into_a_ditto_and_has_no_special_style(self):
        html = bv.build_html(*self.main_built())
        fn = re.search(r"function dittoSrc\(r,i\)\{.*?\n return -1\}", html, re.S)[0]
        script = ("const D=%s;const DEGREE_NAMES=['学士','修士','博士'];const CI=n=>D.columns.indexOf(n);const shown=()=>true;%s;"
                  "const r=D.columns.map(()=>['≠',null,'','x']);const same=D.columns.map(()=>['30.0',30,'']);"
                  "console.log(JSON.stringify([dittoSrc(r,CI('修士初任給')),dittoSrc(same,CI('修士初任給')),CI('学士初任給')]))"
                  % (json.dumps({"columns": bv.COLUMNS, "fold": [bv.fold_role(c) for c in bv.COLUMNS]}, ensure_ascii=False), fn))
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [-1, COL["学士初任給"], COL["学士初任給"]])
        self.assertNotIn("td.ne{", html)  # 「—」と同じ na のスタイル
        self.assertNotIn("td.co{", html)  # 会社行だけの背景は付けない
        self.assertIn("c[0]==='—'||c[0]===NE?' na':''", html)

    def test_rows_carry_the_assigned_company_id_that_groups_them(self):
        rows = self.main_built()[0]
        self.assertEqual([r[-1]["p"] for r in rows], ["P"] * 3 + ["Q"] * 2 + ["R"] + ["T"] * 2 + ["U"] * 2)
        self.assertNotIn("pc", rows[0][-1])
        payload = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',
                                       bv.build_html(*self.main_built()), re.S)[1])
        self.assertEqual(payload["name_list"], bv.NAME_LIST)
        self.assertNotIn("slot_label", payload)

    def test_panel_of_a_company_row_uses_the_company_cells(self):
        out = self.run_js(self.filter_built(), """
render();const cr=shown()[0],d=cr[NC];
console.log(JSON.stringify({title:d.title,pub:d.pub,mixed:d.mixed,windows:d.windows.length,sel:label(selected)}))
""")
        self.assertEqual(out["title"], ["配属A", "甲コース", "乙コース", "丙別枠"])
        self.assertEqual(out["pub"], "採用元F")
        self.assertFalse(out["mixed"])  # 工程がどれも同じ（なし）なら、そのまま
        self.assertEqual(out["windows"], 2)
        self.assertEqual(out["sel"], "会社:甲コース／乙コース／丙別枠")


class EncryptionTest(unittest.TestCase):
    PASSWORD = "dummy-pass-123"

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
        rows, groups, companies, html = self.built()
        e = self.payload(html)
        self.assertEqual((e["alg"], e["hash"], e["iterations"]), ("AES-256-GCM", "SHA-256", 200_000))
        salt, iv, ct = (base64.b64decode(e[k]) for k in ("salt", "iv", "ct"))
        self.assertEqual((len(salt), len(iv)), (16, 12))
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=e["iterations"]).derive(
            self.PASSWORD.encode())
        import gzip
        self.assertEqual((e["v"], e["zip"]), (2, "gzip"))
        compressed = AESGCM(key).decrypt(iv, ct, None)
        self.assertTrue(compressed.startswith(b"\x1f\x8b"))
        plaintext = gzip.decompress(compressed)
        self.assertLess(len(ct), len(plaintext) // 4)
        data = json.loads(plaintext.decode("utf-8"))
        # 暗号化版の中身は平文版と同一（built_at を除く）。平文版を先に作っても暗号化版が崩れないこと。
        plain = self.payload(bv.build_html(rows, groups, companies))
        self.assertEqual({k: v for k, v in data.items() if k != "built_at"},
                         {k: v for k, v in plain.items() if k != "built_at"})
        self.assertEqual(data["columns"], bv.COLUMNS)

    def test_unsupported_decompression_has_specific_message(self):
        html = self.built()[3]
        self.assertIn("if(!window.DecompressionStream)", html)
        self.assertIn("gzip 展開に対応したブラウザー", html)

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
        self.assertEqual(len(self.payload(html)["rows"]), len(rows))
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
        code, _ = self.run_main(["--plain-output", str(plain), "--output", str(enc)], EncryptionTest.PASSWORD)
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


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # ブラウザーでの確認は、playwright があるときだけ行う
    sync_playwright = None


def many_rows_html(rows=120):
    """スクロールできる行数（応募枠ごとに初任給の違う rows 行）を持つ平文ページ。"""
    slots = [slot(2028, ["A"], [(300000 + i * 1000, "official")], [window("official", "2028-04-01")], name=f"枠{i:03d}")
             for i in range(rows)]
    built = bv.build_rows(PROFILES, [{"recruitment_year": 2028, "publishing_company_id": "P", "application_slots": slots}])
    assert len(built[0]) == rows
    return bv.build_html(*built)


class HoverBandTest(unittest.TestCase):
    """行のホバーを :hover による背景の変更で表すと、ポインターの下を行が通るたび（スクロール中は毎フレーム）に表の層全体を描き直す。"""

    def test_rows_have_no_hover_style_and_the_band_is_a_separate_layer(self):
        html = bv.build_html(*bv.build_rows(PROFILES, []))
        self.assertNotIn("tr:hover td", html)
        self.assertIn("#hv{position:absolute;", html)
        self.assertIn("will-change:transform", re.search(r"#hv\{[^}]*\}", html)[0])
        self.assertIn("showHover(hoverTr)}", html.split("function selectRow(tr){", 1)[1].split("const hd=", 1)[0])


@unittest.skipIf(sync_playwright is None, "playwright is required for browser checks")
class HoverBandBrowserTest(unittest.TestCase):
    ROWS = 120

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "index.html"
        path.write_text(many_rows_html(cls.ROWS), encoding="utf-8")
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:  # ブラウザー本体が未取得の環境
            cls.pw.stop()
            cls.tmp.cleanup()
            raise unittest.SkipTest(f"chromium is not available: {e}")
        cls.page = cls.browser.new_page(viewport={"width": 1400, "height": 900})
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.add_init_script("localStorage.setItem('viewerAssignOpen', JSON.stringify(['A']))")  # 配属先 A（全行）を展開しておく。既定の収納では1行になる
        cls.page.goto(path.as_uri())
        cls.page.wait_for_selector("#tb tr")
        assert cls.page.evaluate("document.querySelectorAll('#tb tr').length") == cls.ROWS

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.tmp.cleanup()

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def under_pointer(self, x, y):
        """ポインターの下の行・帯の位置関係と、そのセルの背景。"""
        return self.page.evaluate(f"""()=>{{const hv=document.getElementById('hv'),h=hv.getBoundingClientRect();
          const el=document.elementsFromPoint({x},{y}).find(e=>e.closest('#tb')),tr=el.closest('tr'),r=tr.getBoundingClientRect();
          return {{hidden:hv.hidden,top:h.top-r.top,height:h.height-r.height,sel:tr.classList.contains('sel'),
                  bg:getComputedStyle(el.closest('td')).backgroundColor,
                  surface:getComputedStyle([...document.querySelectorAll('#tb tr:not(.sel)')].at(-1).cells[2]).backgroundColor}}}}""")

    def test_band_follows_the_row_under_the_pointer_without_changing_cell_styles(self):
        box = self.page.evaluate("()=>{const r=document.querySelector('.wrap').getBoundingClientRect();return [r.left,r.top]}")
        x, y = box[0] + 400, box[1] + 200
        self.page.mouse.move(x, y)
        self.page.wait_for_function("!document.getElementById('hv').hidden")
        b = self.under_pointer(x, y)
        self.assertAlmostEqual(b["top"], 0, delta=1)
        self.assertAlmostEqual(b["height"], 0, delta=1)
        self.assertEqual(b["bg"], b["surface"])  # セルの背景は変えない（描き直しの原因になる）
        for _ in range(8):  # ポインターを動かさずにスクロール → 帯は新しくポインターの下に来た行へ移る
            self.page.mouse.wheel(0, 100)
            self.page.wait_for_timeout(30)
        self.page.wait_for_timeout(400)
        b = self.under_pointer(x, y)
        self.assertFalse(b["hidden"])
        self.assertAlmostEqual(b["top"], 0, delta=1)
        self.page.mouse.move(box[0] + 400, box[1] - 30)  # 表の外へ
        self.page.wait_for_function("document.getElementById('hv').hidden")

    def test_band_stays_on_the_row_under_the_pointer_when_the_rows_are_rearranged(self):
        box = self.page.evaluate("()=>{const r=document.querySelector('.wrap').getBoundingClientRect();return [r.left,r.top]}")
        x, y = box[0] + 400, box[1] + 200
        self.page.mouse.move(x, y)
        self.page.wait_for_function("!document.getElementById('hv').hidden")
        self.page.fill("#q", "枠05")  # 絞り込みで、ポインターの下の行が入れ替わる（ポインターは動かさない）
        self.page.wait_for_timeout(400)
        b = self.under_pointer(x, y)
        self.assertFalse(b["hidden"])
        self.assertAlmostEqual(b["top"], 0, delta=1)
        self.page.fill("#q", "")

    def test_selected_row_keeps_its_selection_color(self):
        box = self.page.evaluate("()=>{const r=document.querySelector('.wrap').getBoundingClientRect();return [r.left,r.top]}")
        x, y = box[0] + 400, box[1] + 300
        self.page.mouse.move(x, y)
        self.page.wait_for_function("!document.getElementById('hv').hidden")
        self.page.mouse.click(x, y)  # 選択すると、その行には帯を重ねない
        self.assertTrue(self.page.evaluate("document.getElementById('hv').hidden"))
        self.assertTrue(self.under_pointer(x, y)["sel"])
        self.page.keyboard.press("Escape")  # セルの根拠ポップアップを閉じる
        self.page.mouse.move(x, y + 70)  # 別の行へ移ると、帯はその行に付く
        self.page.wait_for_function("!document.getElementById('hv').hidden")

    BAND_ROW = """()=>{const hv=document.getElementById('hv'),rows=[...document.querySelectorAll('#tb tr')];
      if(hv.hidden)return {hidden:true,row:-1,sel:rows.findIndex(r=>r.classList.contains('sel'))};
      const h=hv.getBoundingClientRect(),row=rows.findIndex(r=>Math.abs(r.getBoundingClientRect().top-h.top)<1.5);
      return {hidden:false,row,sel:rows.findIndex(r=>r.classList.contains('sel'))}}"""

    def fresh(self):
        """スクロール・選択を初期状態に戻す（クラスで1つのページを使い回すため）。"""
        self.page.reload()
        self.page.wait_for_selector("#tb tr")
        self.page.mouse.move(0, 0)

    def row_top(self, i):
        return self.page.evaluate(f"document.querySelectorAll('#tb tr')[{i}].getBoundingClientRect().top")

    def test_band_over_the_merged_assignment_cell_is_on_the_row_at_the_pointer(self):
        """配属先セルを rowSpan で結合した行では、結合したセルは先頭の行のセル。その上でも帯はポインターの高さの行に付く。"""
        self.fresh()
        left = self.page.evaluate("document.querySelector('.wrap').getBoundingClientRect().left")
        self.assertEqual(self.page.evaluate("document.querySelector('#tb tr').cells[0].rowSpan"), self.ROWS)
        x = left + 60
        self.page.mouse.move(x, self.row_top(0) + 15)  # 先頭の行（既定で選択中）には帯を重ねない
        self.page.mouse.move(x, self.row_top(4) + 15)
        self.page.wait_for_function("!document.getElementById('hv').hidden")
        self.assertEqual(self.page.evaluate(self.BAND_ROW), {"hidden": False, "row": 4, "sel": 0})
        for row in (9, 5, 0):  # 結合したセルの中を動いても（要素は変わらない）、帯は行に追従する
            self.page.mouse.move(x, self.row_top(row) + 15)
            self.page.wait_for_timeout(50)
            expected = {"hidden": True, "row": -1, "sel": 0} if row == 0 else {"hidden": False, "row": row, "sel": 0}
            self.assertEqual(self.page.evaluate(self.BAND_ROW), expected)

    def test_band_never_covers_the_selected_row_when_the_selection_moves_by_expanding_and_collapsing(self):
        self.fresh()
        left = self.page.evaluate("document.querySelector('.wrap').getBoundingClientRect().left")
        x = left + 60
        self.page.mouse.move(x, self.row_top(0) + 15)
        self.page.mouse.click(x, self.row_top(0) + 15)  # 結合したセルのクリック → 収納。選択は会社行へ移る
        self.page.wait_for_function("document.querySelectorAll('#tb tr').length===1")
        self.assertTrue(self.page.evaluate("document.querySelector('#tb tr').cells[0].textContent").startswith(f"+{self.ROWS}"))
        self.page.wait_for_timeout(200)
        state = self.page.evaluate(self.BAND_ROW)
        self.assertEqual((state["row"], state["sel"]), (-1, 0))  # 会社行は選択中なので帯は重ならない
        self.page.mouse.click(x, self.row_top(0) + 15)  # 会社行のセルのクリック → 展開。選択は先頭のメンバー行へ移る
        self.page.wait_for_function(f"document.querySelectorAll('#tb tr').length=={self.ROWS}")
        self.page.wait_for_timeout(200)
        state = self.page.evaluate(self.BAND_ROW)
        self.assertEqual((state["row"], state["sel"]), (-1, 0))
        self.page.mouse.move(x, self.row_top(3) + 15)  # 別の行へ移れば帯が付く
        self.page.wait_for_function("!document.getElementById('hv').hidden")
        self.assertEqual(self.page.evaluate(self.BAND_ROW), {"hidden": False, "row": 3, "sel": 0})


if __name__ == "__main__":
    unittest.main()
