指定された 1 社について、`recruitment_case` を公開情報だけで埋めてください。

前提:

* 入力として `company_id`、`legal_name`、`recruitment_year` が与えられます。
* 出力は `schemas/recruitment_case_models.py` の `RecruitmentCase` に従う単一 JSON です。
* 共通調査契約、Recruitment Case 共通品質契約、および schema に従ってください。

作業:

1. 対象企業の採用ページ、募集要項、採用 FAQ、募集案内 PDF、就活サイト掲載情報から、入力された`recruitment_year`とその前年度の新卒本選考の応募枠を調査し、`application_slot_inventory`を確定する。
2. 各 slot を source ごとに埋める。
   - 検索したか、指定された collected output path に書き込んだかは監視されています。

出力:

* 指定された collected output path に、単一の `RecruitmentCase` JSON を書き込む。
* JSON は collected output path だけに保存し、最終応答へ再掲しない。
* file の書込み後、最終応答は完了したことだけを短い一文で返す。
