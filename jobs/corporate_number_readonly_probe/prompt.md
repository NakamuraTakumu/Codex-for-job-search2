# corporate_number_readonly_probe

この job は、`scripts/corporate_number_lookup.py` が read-only の read path だけで SQLite DB にアクセスできるかを確認する。

次を実行して結果を確認する。

1. `python3 scripts/corporate_number_lookup.py stats`
2. `python3 scripts/corporate_number_lookup.py lookup 1010001128061`

制約:

- write file は作らない。
- 失敗した場合は、どの command がどの error で落ちたかを短く報告する。
- 成功した場合は、`stats` の `row_count` と `lookup` の `legal_name` を含めて簡潔に報告する。
