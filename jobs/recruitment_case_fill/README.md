# recruitment_case_fill

`recruitment_case_fill` は、法人番号である `company_id`、法人名 `legal_name`、入社年度 `recruitment_year` を入力にして、単一企業の `recruitment_case` JSON を生成する wrapper job である。

生成する JSON は、入社年度を対象とする新卒本選考の応募枠、募集期間、初任給、リモート可否、出典付きの選考プロセスを埋める。同じ応募枠に早期選考や複数回締切がある場合は、`application_windows`で開始日と締切日の組を募集回ごとに保持する。

## Usage

```python
from jobs.common.codex_job import build_session_config
from jobs.recruitment_case_fill import RecruitmentCaseFillJob

config = build_session_config(
    cwd="<repository root>",
    timeout=1800,
)

response = RecruitmentCaseFillJob(
    config=config,
    company_id="1180301018771",
    legal_name="トヨタ自動車株式会社",
    recruitment_year=2028,
).run()

print(response.files["recruitment_case.json"].text)
```

## Contract

- Main の既定は GPT-6 Luna high、automatic reviewer は GPT-6 Luna high、調査子は GPT-6 Luna high である。`model` と `reasoning_effort` を渡すと Main だけを明示上書きできる
- `${SUBCWD}/recruitment_case.json` を成果物の正本とし、最終応答にはJSONを含めない
- Runner管理subagentを使うかはmainとreviewerの判断に任せ、promptでは使い方を指示しない
- 規則の正本はschema、`quality_contract.md`、`jobs/common/research_prompt.md`（共通調査契約）とし、mainとreviewerの両方へ同じものを渡す。domain promptは作業と出力だけを持つ
- 子はGPT-6 Luna low固定で、最大3件並行とする
- 子は親sessionをforkせず、親が`spawn_agent`のmessageへ必要な調査taskを明示して渡す
- automatic reviewではmainとreviewerが`ask_peer`で必要な情報を一往復だけ照会できる
- mainの子はwrite pathを持たず、fileを変更せず最終応答で親へ結果を返す
- reviewerの親子はread-onlyとし、正本の書込みと最終的な合否判断は親だけが行う
- `RecruitmentCase` Pydantic model はcollected JSONのvalidatorで使う
- validator はcollected JSONのPydantic validationと入力値保持チェックを行う
- jobはJSONを`response.files["recruitment_case.json"]`として回収する
- `publishing_company_id` は入力の `company_id` と一致させる
- `recruitment_year` は入力値を保持する
- 各slotの `application_year` は source が示す年度とし、年度の記載がない source は対象年度（`recruitment_year`）の情報として扱う
- `application_slots` は確認できた応募枠を持ち、対象年度と前年度を調査しても実在する新卒本選考の応募枠を確認できない場合は空配列を持つ
- 成果物ファイルなしを調査未実施、入力値を保持した成果物の`application_slots=[]`を調査済みだが応募枠不在として区別する
- 年度が異なる値やsourceは同じslotへ混ぜず、同名でも`application_year`が異なる応募枠は別slotにする
- 対象年度と`recruitment_year - 1`の両方を調査し、前年度情報はsource上の`application_year`を持つ別slotとして保存する
- さらに古い年度は調査しない
- `application_windows` は、確認できた募集回の一覧または募集期間を確認できなかった理由を持つ
- slotの分け方は schema の `application_slots` を正本とする。応募が別なら別slot、日程だけが異なる募集回は同じslotの`application_windows`にする
- `selection_process.process` は `sourced_metric` として表し、found の `records[].value` は `{ "stages": [{ "steps": [{ "kind": ..., "label": ... }] }, ...] }` に従う。stage は source の番号や矢印などの区切りをそのまま写す
- `entry_sheet_submission` はES、企業指定の応募フォーム、通常のES設問への回答提出を表す
- `written_assignment` はESとは独立して評価される小論文、作文、レポート、長文記述課題を表し、ES提出と併存できる
- 書類選考は`entry_sheet_submission`にも`written_assignment`にも含めず、内容不明の応募書類提出もどちらかへ推測変換しない
- `group_interview` はグループ面接・集団面接、`group_discussion` は候補者同士の討議として区別する
- `existing_output_path` を渡すと改訂モードになる。既存ファイルを `read_paths` に加え、入力に既存結果のパスを示し、`jobs/common/revise_prompt.md`（既存を出発点に加筆し、誤りだけ修正する）を fixed_context に加える。既存ファイルは旧 schema 版の形式でもよく、その場合は現行 schema へ変換して検証に通す（clientは既存ファイルを検証せず参照として渡すだけ）。reviewは通常どおり全体を対象にする

- `application_open_date` / `application_deadline_date` は `DateRange`（earliest, latest）で、上旬・中旬・下旬・月だけの表記も範囲として記録する。非公式 source は常に調べ、公式 record とは別 record で保持する
