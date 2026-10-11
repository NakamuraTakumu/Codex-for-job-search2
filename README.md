# company_survey

就活向けの企業調査を、スクリプト経由で `company_profile` JSON と法人番号データベースに落とす workspace です。

> 注意: 調査の実行には、外部の Codex job runner（`codex_job_runner`、`~/.codex/skills/codex-job-runner` から import）と `openai_codex` パッケージが必要です。これらはこのリポジトリには含まれていません。

## 目的

- 単一企業の `company_profile` を `data/companies/<company_id>.json` として保存する。`company_id` は 13桁の法人番号を使う。
- 法人番号の候補検索と正引きを、ローカル DB で行う。
- 採用枠や募集年度の情報は `recruitment_case` として別 schema で扱う。

## 主要スクリプト

### `scripts/fill_company_profile.py`

単一企業の `company_profile` を生成します。第1引数は 13桁の法人番号である `company_id` で、出力ファイル名も `<company_id>.json` になります。

```bash
python3 scripts/fill_company_profile.py 1180301018771 "トヨタ自動車株式会社"
```

標準では `tmp/<timestamp>_<company_name>.json` に保存します。上の例では `tmp/20260606T...Z_トヨタ自動車株式会社.json` のようなファイル名になります。保存先を明示したい場合は `--output` を使います。

```bash
python3 scripts/fill_company_profile.py 1180301018771 "トヨタ自動車株式会社" --output tmp/custom.json
```

追加の調査指示を与えたい場合:

```bash
python3 scripts/fill_company_profile.py 1180301018771 "トヨタ自動車株式会社" \
  --extra-instructions "IR資料を優先して確認する"
```

### `scripts/fill_recruitment_case.py`

単一企業の `recruitment_case` を生成します。第1引数は 13桁の法人番号である `company_id`、第2引数は法人名、 第3引数は対象の `recruitment_year` です。

`recruitment_case_fill` runnerを1回実行し、募集枠の発見から各`ApplicationSlot`の調査までを行います。同じ応募枠に早期選考や複数回締切がある場合は、`application_windows`で募集回ごとの開始日と締切日の対応を保持します。開始日・締切日が未定／後日案内と出典に明示される場合は、日付の`records`を空にして`announced_later`に`source`と`retrieval`を保持し、`note`も記録します。日付の記載が見つからない通常のMissingと区別し、閲覧ページには根拠付きの「後日案内」と表示します。日付の記録と`announced_later`は併存できません。

```bash
python3 scripts/fill_recruitment_case.py 1180301018771 "トヨタ自動車株式会社" 2028
```

標準では `tmp/<timestamp>_<company_name>_<recruitment_year>.json` に保存します。保存先を明示したい場合は `--output` を使います。

```bash
python3 scripts/fill_recruitment_case.py 1180301018771 "トヨタ自動車株式会社" 2028 \
  --output tmp/custom_recruitment.json
```

### `scripts/fill_recruitment_cases.py`

複数企業の `recruitment_case` を上限付きで並列生成します。
入力JSONの `companies` 配列には、`company_id` と `legal_name` を含めます。
同じ `company_id` は先頭の1件だけを実行します。

```bash
python3 scripts/fill_recruitment_cases.py \
  tmp/published_companies.json \
  2028 \
  --limit 10 \
  --max-workers 10 \
  --warmup-jobs 1 \
  --job-timeout 1800
```

各企業のJSONと全件の成否、利用量、log pathを含む `summary.json` を、timestamp付きの出力directoryへ保存します。
並列実行には `CodexWorkerPool` を使用します。
poolは最初の`warmup_jobs`件を完了してから、残りを`max_workers`以下で実行します。
jobが失敗しても残りのjobは続行し、summaryでは失敗した対象を`failed`、batch自体が結果を返さなかった場合の未完了対象を`batch_failed_or_cancelled`として記録します。

### `scripts/survey_companies.py`

企業リストの全調査を、`recruitment_case` 先行で行います。

1. リストの各公表法人について `recruitment_case` を生成し、`data/recruitment_cases/<company_id>_<recruitment_year>.json` に保存する。
2. 保存済みの同年度の全 `recruitment_case` から `publishing_company_id` と `assignment_companies.company_ids` を集め、法人番号 DB の登記名（NFKC 正規化）で `company_profile` を生成し、`data/companies/<company_id>.json` に保存する。

保存済みの出力は飛ばすため、失敗後は同じコマンドを再実行すれば続きから進みます。summary と失敗時の診断出力は `tmp/survey_<recruitment_year>_<timestamp>/` に保存します。DB に見つからない法人番号は実行せず、summary の `unregistered_company_ids` に記録します。

`--revise` は保存済みの出力を飛ばさず、既存ファイルを出発点に再調査します。既存ファイルが旧 schema 版の形式（業種コードが単一値、応募開始日・締切日が特定日の値など）でも、そのまま参照として渡し、モデルが現行 schema へ変換します（データを変換するスクリプトはありません）。

`--only-invalid`（`--revise` が必須）は、既存の出力のうち現行 schema の検証に通らないものだけを対象にします。対象は、リストの法人の `recruitment_case`（指定年度）と、それらが参照する `company_profile` で、存在しない出力や検証に通る出力は対象外です。変換に失敗した企業だけを再実行したいときに使い、選ばれた件数を stderr に表示します。

```bash
python3 scripts/survey_companies.py list.json 2028 --revise --only-invalid
```

```bash
python3 scripts/survey_companies.py \
  tmp/survey_companies_2028.json \
  2028 \
  --max-workers 5
```

### `scripts/corporate_number_lookup.py`

法人番号 DB を参照専用で検索します。`search` / `lookup` / `stats` だけを持ち、read-only 環境でも使えます。

会社名で検索する場合:

```bash
python3 scripts/corporate_number_lookup.py search "トヨタ自動車株式会社" --company-only --limit 5
```

法人番号で正引きする場合:

```bash
python3 scripts/corporate_number_lookup.py lookup 1180301018771
```

同期済み DB の状態を確認する場合:

```bash
python3 scripts/corporate_number_lookup.py stats
```

### `scripts/corporate_number_sync.py`

国税庁データを同期し、法人番号 DB を更新します。`sync` / `import-csv` だけを持つ write-capable 入口です。

全国データを同期する場合:

```bash
python3 scripts/corporate_number_sync.py sync --all
```

都道府県別に同期する場合:

```bash
python3 scripts/corporate_number_sync.py sync --prefecture 東京都
```

### `scripts/corporate_number_resolver.py`

`corporate_number_lookup.py` と `corporate_number_sync.py` の互換入口です。既存利用向けに残しています。

## 閲覧ページのビルド

```bash
# 平文のみ（ローカル確認用）
python3 scripts/build_viewer.py

# 平文 + 暗号化（公開用）
REPORT_ACCESS_PASSWORD='合言葉' python3 scripts/build_viewer.py
```

公開するときは、`REPORT_ACCESS_PASSWORD` を設定して `python3 scripts/publish_viewer.py` を実行します。暗号化版を生成し、`gh-pages` ブランチの `index.html` としてコミットします。公開は `git push origin gh-pages` で行います。

1回のビルドで同じデータから2種類の HTML を出力します。

- `--plain-output`（標準 `tmp/viewer/index.html`）: 暗号化なしでデータを埋め込んだローカル確認用。合言葉の入力なしですぐ開き、`python3 -m http.server` などの通常の HTTP で見られます。毎回生成します。`tmp/` は `.gitignore` 済みで、平文はここにだけ置きます。
- `--output`（標準 `site/index.html`）: 公開用の暗号化版（PBKDF2-HMAC-SHA256 → AES-256-GCM）。環境変数 `REPORT_ACCESS_PASSWORD` が設定されていて空でないときだけ生成します。未設定なら標準エラー出力に通知を出し、平文のみ生成して終了コード 0 で終わります。

平文の出力先が git の管理外（`git check-ignore` で ignore 済み）でない場合、またはリポジトリ外の場合は、何も書き込まずエラー（終了コード 1）で終了します。平文データの誤コミットを防ぐためで、回避用のオプションはありません。

## 保存先

- `company_profile` 出力先: `data/companies`
- `recruitment_case` 出力先: `data/recruitment_cases`
- 法人番号 cache / DB: `data/cache/corporate_number`
- `company_profile` schema model: `schemas/company_profile_models.py`
- `recruitment_case` schema model: `schemas/recruitment_case_models.py`

## データ責務

- `company_profile`: 企業そのものの基本情報、指標、出典を保持する。
- `recruitment_case`: 入社年度と応募年度つきの新卒本選考の応募枠、配属先企業群、募集期間、選考スコープなど採用枠の情報を保持する。両年度は独立しており、一致を要求しない。配属先企業は `company_ids` だけで表す。同じ応募枠内の早期選考や複数回締切は`application_windows`、給与とリモート可否は`sourced_metric`、選考の有無は`has_*`、選考段階構造は`selection_process.process`として記録する。

採用情報は `company_profile` に混ぜず、必要なら `recruitment_case` 側へ分離します。
