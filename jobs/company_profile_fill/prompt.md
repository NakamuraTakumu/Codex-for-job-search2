指定された 1 社について、`company_profile` を公開情報だけで埋めてください。

前提:
- 入力として `company_id` と `legal_name` が与えられます。
- `company_id` は対象法人の13桁の法人番号です。
- 出力は `schemas/company_profile_models.py` の `CompanyProfile` に従う単一企業 JSON です。
- 共通調査契約と schema に従ってください。

作業:
1. 対象企業の `basic_information` の各 metric を調査する。

調査ルール:
- 出力の `company_id` と `legal_name` は入力値をそのまま使う。

出力:
- 指定された collected output path に、単一企業の `CompanyProfile` JSON を書き込む。
- JSON は collected output path だけに保存し、最終応答へ再掲しない。
- file の書込み後、最終応答は完了したことだけを短い一文で返す。
