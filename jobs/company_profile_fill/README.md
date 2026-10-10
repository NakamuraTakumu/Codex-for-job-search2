# company_profile_fill

`company_profile_fill` は、法人番号である `company_id` と `legal_name` を入力にして、単一企業の `company_profile` JSON を返す wrapper job である。

返却する JSON は 1 社 1 ファイル運用を前提としており、`basic_information` を中心に補完する。

## Usage

```python
from jobs.common.codex_job import build_session_config
from jobs.company_profile_fill import CompanyProfileFillJob

config = build_session_config(
    cwd="<repository root>",
    timeout=1800,
)

response = CompanyProfileFillJob(
    config=config,
    company_id="1180301018771",
    legal_name="トヨタ自動車株式会社",
).run()

print(response.files["company_profile.json"].text)
```

## Contract

- Main の既定は GPT-6 Luna high、automatic reviewer は GPT-6 Luna high、調査子は GPT-6 Luna high である。`model` と `reasoning_effort` を渡すと Main だけを明示上書きできる
- `${SUBCWD}/company_profile.json` を成果物の正本とし、最終応答にはJSONを含めない
- `CompanyProfile` Pydantic model はcollected JSONのvalidatorで使う
- validator はcollected JSONのPydantic validationと入力値保持チェックを行う
- automatic reviewではmainとreviewerが`ask_peer`で必要な情報を一往復だけ照会できる
- 規則の正本はschemaと`jobs/common/research_prompt.md`（共通調査契約）とし、mainとreviewerの両方へ同じものを渡す
- Runner管理subagentを使うかはmainとreviewerの判断に任せ、promptでは使い方を指示しない。子はGPT-6 Luna low固定で、親sessionをforkせず、write pathを持たない
- `company_id` は 13桁の法人番号として扱い、`legal_name` とともに入力値を保持する
- `basic_information` の期待型と enum 許可値は schema 注釈を参照して validator で追加検証する
- `basic_information` には研究開発費と研究公開/産学連携の指標が含まれる。博士支援と学会協賛は扱わない
- `existing_output_path` を渡すと改訂モードになる。既存ファイルを `read_paths` に加え、入力に既存結果のパスを示し、`jobs/common/revise_prompt.md`（既存を出発点に加筆し、誤りだけ修正する）を fixed_context に加える。既存ファイルは旧 schema 版の形式でもよく、その場合は現行 schema へ変換して検証に通す（clientは既存ファイルを検証せず参照として渡すだけ）。reviewは通常どおり全体を対象にする

- 業種コードと応募開始日・締切日の形式、および非公式 source を常に調べ、公式 record とは別 record で保持する方針は schema と `jobs/common/research_prompt.md` に従う
