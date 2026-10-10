# Recruitment Case 共通品質契約

生成者とreviewerは、`RecruitmentCase`の生成または監査を完了する前に、次の不変条件をすべて確認する。

- JSONへ採用した各sourceは、対象法人、slot、fieldの値を直接支える。年度はschemaの`application_year`に従う。
- selection sourceに記載された応募者の必須活動を欠落させない。schemaのenumに対応する値がない活動も、`other`として記録する。採用担当者側だけが行う社内処理、合否判定、結果通知は応募者の活動に含めない。
- 年度が異なる値やsourceを同じslotへ混ぜない。
- slotは、schemaの`application_slots`の分け方に従って過不足なく分ける。
- `application_windows`の各windowの開始日と締切日は同じ募集回に対応させる。複数windowが共通の開始日を持ち締切だけが異なる場合は、同じ開始日を各windowへsource付きで記録する。
- `application_slot_inventory`はslot探索結果の正本とし、各recordのsourceが`slot_name`と新卒本選考枠であることを直接支える。`application_slots`はinventoryと同じ順序で作り、identity集合を`application_year + slot_name`で完全一致させ、identityを重複させない。対象年度と前年度のどちらでも新卒本選考の応募枠を確認できなかった場合だけ、inventoryのrecordsを空にし、`application_slots=[]`とする。
- 対象年度と前年度（`recruitment_year - 1`）の両方を調査する。前年度の応募枠は、前年度sourceが示す応募枠単位とsource上の`application_year`のまま、別の`ApplicationSlot`として記録する。対象年度から枠名や区分を推測せず、さらに古い年度は調査しない。
- 確認した全slotについて、schemaが要求する全fieldを確認する。値を確定できないfieldも省略せず、schemaと共通調査契約に従ってMissingを記録する。
- `publishing_company_id`と`recruitment_year`は入力を保持し、最終JSON全体がschemaに適合する。
