"""Pydantic models corresponding to recruitment_case.schema.json."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from .common_models import (
    NOTE_DESCRIPTION,
    CompanyId,
    SchemaModel,
    Source,
    SourcedMetric,
    require_note_when_empty,
)


MetricNumber = float


class SelectionProcessStep(StrEnum):
    """Source-confirmed activities that the candidate performs during selection."""

    WEB_TEST = "web_test"
    WRITTEN_EXAM = "written_exam"
    ENTRY_SHEET_SUBMISSION = "entry_sheet_submission"
    WRITTEN_ASSIGNMENT = "written_assignment"
    GROUP_DISCUSSION = "group_discussion"
    GROUP_INTERVIEW = "group_interview"
    INTERVIEW = "interview"
    CASE_INTERVIEW = "case_interview"
    RESEARCH_PRESENTATION = "research_presentation"
    CODING_TASK = "coding_task"
    OTHER = "other"


class AssignmentCompanies(SchemaModel):
    company_ids: list[CompanyId] = Field(
        description="配属先企業として明示または候補提示されている法人番号一覧。未指定の場合は空配列。"
    )


class SelectionStep(SchemaModel):
    kind: SelectionProcessStep = Field(
        description="工程の種類。source の工程名に対応する値がない場合は other にする。",
        json_schema_extra={
            "x-do_not": "source の工程名を、意味が近いだけの enum 値へ置き換えない。"
        },
    )
    label: str = Field(
        min_length=1,
        description="source 上の工程名。",
    )


class SelectionProcessStage(SchemaModel):
    steps: list[SelectionStep] = Field(
        min_length=1,
        description=(
            "source が一つの番号や矢印などの区切りにまとめて記載した、応募者が必ず行う工程。"
            "source の記載順に並べる。"
        ),
        json_schema_extra={
            "x-do_not": (
                "採用側だけが行う書類確認、採点、合否判断、結果通知を step にしない。"
                "任意参加の説明会など、応募者が必ず行うとは限らない工程を step にしない。"
            )
        },
    )


class SelectionProcessValue(SchemaModel):
    stages: list[SelectionProcessStage] = Field(
        min_length=1,
        description=(
            "応募者が行う選考工程を、source の記載順に、source の番号や矢印などの区切りで"
            "分けた段階の一覧。"
        ),
        json_schema_extra={
            "x-do_not": (
                "source が順序や区切りを示していない工程の列挙から stage を作らない。"
                "一般的な選考順から順序や区切りを推測しない。"
            )
        },
    )


class SelectionProcess(SchemaModel):
    process: SourcedMetric[SelectionProcessValue] = Field(
        description=(
            "応募者が行う選考工程の順序。source が工程の順序を示さない場合は Missing にする。"
            "面接回数の幅など、stage で表せない事情は note に書く。"
        ),
        json_schema_extra={
            "x-do_not": "別募集枠専用の選考工程 source を流用しない。"
        },
    )
    has_web_test: SourcedMetric[bool] = Field(
        description="応募枠で、sourceがWebテストまたはオンライン受検と明示する検査があるかどうか。"
    )
    has_written_exam: SourcedMetric[bool] = Field(
        description=(
            "応募枠で、Webテストと明示されない筆記試験または適性検査があるかどうか。"
            "テストセンターや会場での受検、受検形式が明示されない適性検査を含む。"
        )
    )
    has_entry_sheet_submission: SourcedMetric[bool] = Field(
        description=(
            "応募枠で、応募者がエントリーシートまたは企業所定の応募フォームを"
            "提出する工程があるかどうか。"
        ),
        json_schema_extra={
            "x-do_not": (
                "採用側だけが行う書類確認や書類選考を根拠に true にしない。"
                "小論文、作文、レポート、その他の独立して評価される記述課題は"
                "has_written_assignment に記録する。"
            )
        },
    )
    has_written_assignment: SourcedMetric[bool] = Field(
        description=(
            "応募枠で、小論文、作文、レポート、その他の独立して評価される記述課題を"
            "応募者が提出する工程があるかどうか。"
        ),
        json_schema_extra={
            "x-do_not": (
                "エントリーシートや企業所定の応募フォームの提出だけを根拠に true にしない。"
                "採用側だけが行う書類確認や書類選考を記述課題として扱わない。"
            )
        },
    )
    has_group_discussion: SourcedMetric[bool] = Field(
        description="応募枠でグループディスカッションがあるかどうか。候補者同士の討議・ディスカッションを指し、グループ面接や集団面接は含めない。",
        json_schema_extra={
            "x-do_not": "「グループ面接」「集団面接」だけを根拠に true にしない。候補者同士の討議、ディスカッション、GD が source に明記される場合だけ true にする。"
        },
    )
    has_group_interview: SourcedMetric[bool] = Field(
        description="応募枠でグループ面接または集団面接があるかどうか。複数候補者が同時に面接官から質問される面接形式を指し、候補者同士の討議は含めない。",
        json_schema_extra={
            "x-do_not": "候補者同士の討議、ディスカッション、GD を根拠に true にしない。グループディスカッションは has_group_discussion に記録する。"
        },
    )
    has_interview: SourcedMetric[bool] = Field(description="応募枠で面接系工程があるかどうか。個人面接、グループ面接、ケース面接などを含む。")
    has_case_interview: SourcedMetric[bool] = Field(
        description="応募枠でケース面接があるかどうか。応募者に課題を与え、その場で問題分解や仮説立てを行わせる面接。"
    )
    has_research_presentation: SourcedMetric[bool] = Field(description="応募枠で研究紹介、研究発表、技術プレゼンがあるかどうか。")
    has_coding_task: SourcedMetric[bool] = Field(description="応募枠でコーディング試験・課題があるかどうか。")

    @model_validator(mode="after")
    def validate_process_bool_consistency(self) -> SelectionProcess:
        present_steps = {
            step.kind
            for record in self.process.records
            for stage in record.value.stages
            for step in stage.steps
        }
        step_fields = {
            SelectionProcessStep.WEB_TEST: ("has_web_test",),
            SelectionProcessStep.WRITTEN_EXAM: ("has_written_exam",),
            SelectionProcessStep.ENTRY_SHEET_SUBMISSION: (
                "has_entry_sheet_submission",
            ),
            SelectionProcessStep.WRITTEN_ASSIGNMENT: ("has_written_assignment",),
            SelectionProcessStep.GROUP_DISCUSSION: ("has_group_discussion",),
            SelectionProcessStep.GROUP_INTERVIEW: (
                "has_group_interview",
                "has_interview",
            ),
            SelectionProcessStep.INTERVIEW: ("has_interview",),
            SelectionProcessStep.CASE_INTERVIEW: (
                "has_case_interview",
                "has_interview",
            ),
            SelectionProcessStep.RESEARCH_PRESENTATION: (
                "has_research_presentation",
            ),
            SelectionProcessStep.CODING_TASK: ("has_coding_task",),
            SelectionProcessStep.OTHER: (),
        }
        contradictions = []
        for step in present_steps:
            for field_name in step_fields[step]:
                records = getattr(self, field_name).records
                if records and all(record.value is False for record in records):
                    contradictions.append(f"{step.value} -> {field_name}")

        if contradictions:
            raise ValueError(
                "selection process steps contradict Found false metrics: "
                + ", ".join(sorted(contradictions))
            )
        return self


_WINDOW_DATE_RANGE_RULES = (
    "日付は DateRange（earliest, latest。各 YYYY-MM-DD）で記録し、時刻は記録しない。"
    "sourceに年の記載がない場合は、そのsourceが対象とする卒業年度や募集時期から年を補う。"
    "sourceの表記が示す範囲を広げたり狭めたりしない。対応づけ: 日まで特定できる場合は"
    " earliest と latest を同じ日にする。上旬・初旬は1日〜10日、中旬は11日〜20日、"
    "下旬は21日〜その月の末日、月だけの記載はその月の1日〜末日とする。"
)
_WINDOW_DATE_DO_NOT = "別の募集回や応募枠の日程を流用しない。"


class DateRange(SchemaModel):
    earliest: date = Field(description="日付として取りうる最も早い日。")
    latest: date = Field(description="日付として取りうる最も遅い日。日まで特定できる場合は earliest と同じ。")

    @model_validator(mode="after")
    def validate_order(self) -> DateRange:
        if self.earliest > self.latest:
            raise ValueError("date range earliest must not be after latest")
        return self


class ApplicationWindow(SchemaModel):
    window_name: str | None = Field(
        description=(
            "同じ応募枠内の募集回名称。早期選考、第1回、第2回などsource上の名称を記録し、"
            "名称がない場合はnullにする。"
        ),
        json_schema_extra={"x-do_not": "source上にない募集回名称や順番を推測で作らない。"},
    )
    source: Source = Field(
        description="この募集回が同じ応募枠に属することと、window_nameの直接根拠。"
    )
    application_open_date: SourcedMetric[DateRange] = Field(
        description=(
            f"この募集回の応募開始日。{_WINDOW_DATE_RANGE_RULES}"
            "随時、定員に達し次第など、開始日を定めない表記は Missing にする。"
        ),
        json_schema_extra={"x-do_not": _WINDOW_DATE_DO_NOT},
    )
    application_deadline_date: SourcedMetric[DateRange] = Field(
        description=(
            f"この募集回の応募締切日。{_WINDOW_DATE_RANGE_RULES}"
            "随時、定員に達し次第など、締切日を定めないことをsourceが示す場合は"
            " Missing にする。"
        ),
        json_schema_extra={"x-do_not": _WINDOW_DATE_DO_NOT},
    )

    @model_validator(mode="after")
    def validate_same_source_date_order(self) -> ApplicationWindow:
        open_by_url = {
            record.source.url: record.value
            for record in self.application_open_date.records
        }
        deadline_by_url = {
            record.source.url: record.value
            for record in self.application_deadline_date.records
        }
        reversed_urls = sorted(
            url
            for url in open_by_url.keys() & deadline_by_url.keys()
            if open_by_url[url].earliest > deadline_by_url[url].latest
        )
        if reversed_urls:
            raise ValueError(
                "application window open date is after deadline for source URLs: "
                + ", ".join(reversed_urls)
            )
        return self


class ApplicationWindows(SchemaModel):
    records: list[ApplicationWindow] = Field(
        description=(
            "同じ応募枠内で確認できた募集期間の一覧。source上の募集回の順に並べる。"
            "募集期間を確認できない（Missing）場合は空配列にする。"
        ),
    )
    note: str | None = Field(default=None, description=NOTE_DESCRIPTION)

    @model_validator(mode="after")
    def validate_windows(self) -> ApplicationWindows:
        require_note_when_empty(self.records, self.note)
        serialized = [
            window.model_dump(mode="json") for window in self.records
        ]
        for index, window in enumerate(serialized):
            if window in serialized[:index]:
                raise ValueError(
                    "application windows contains an exact duplicate object"
                )
        return self


_APPLICATION_YEAR_DESCRIPTION = (
    "応募枠が対象とする卒業予定年兼入社年。sourceが示す年度（「28卒」「2028年卒業予定」"
    "「2028年入社」など）を使う。年度の記載がないsourceだけで確認した応募枠は、閲覧時点で"
    "掲載中の募集としてrecruitment_yearの枠にし、そのことをnoteに書く。各値は応募枠の"
    "年度が明記されたsourceを優先し、年度の記載がないsourceは、年度が明記されたsourceが"
    "ない項目に限ってどちらの年度の枠にも使ってよい。使った場合はnoteに書く。"
    "卒業予定年または入社年が異なる同名の応募枠は、別のApplicationSlotとして保持する。"
)
_APPLICATION_YEAR_DO_NOT = (
    "別の年度の応募枠を対象とするsourceを使わない。年度が明記されたsourceがある項目に、"
    "年度の記載がないsourceの値を使わない。応募受付の暦年や受付日から決めず、"
    "募集開始年の翌年と固定して計算しない。"
)
_SALARY_SCOPE = (
    "形式化するのは月給額・基本給・固定残業代で、それ以外の手当、年俸制、"
    "条件による金額差などはnoteに書く。"
)
_SALARY_MULTIPLE_AMOUNTS = (
    "「～」などの下限表記や、勤務地・コースなどの条件で金額が複数ある場合は、"
    "最低額を記録し、詳細をnoteに書く。"
)
_SALARY_DEGREE_MAPPING = (
    "学歴別の額があればそれぞれの額を使う。学歴の区分を付けずに示された額（学歴不問、"
    "または区分の記載なし）は、学士・修士・博士のそれぞれに記録し、区分の記載がないことを"
    "noteに書く。「院卒」「大学院修了」のように複数の学歴をまとめた区分の額は、その区分に"
    "含まれる学歴（院卒なら修士・博士）のそれぞれに記録する。ただし、その学歴が応募対象外、"
    "または個別設定・別の額とsourceが示していれば記録しない。「大卒」など1つの学歴の額だけが"
    "示されている場合、他の学歴には記録しない。"
)
_SALARY_DESCRIPTION_TAIL = _SALARY_SCOPE + _SALARY_MULTIPLE_AMOUNTS + _SALARY_DEGREE_MAPPING
_SALARY_DO_NOT = (
    "年収換算や推定値を入れない。別募集枠の給与 source を流用しない。"
    "下限表記や条件別の金額を records に並べない。"
)


class StartingSalary(SchemaModel):
    """1つの応募枠・1つの学歴についてsourceが示す月額初任給。monthly_total_yen が月給の総額で、base_salary_yen と fixed_overtime_yen はその内訳。"""

    monthly_total_yen: MetricNumber | None = Field(
        default=None,
        description=(
            "月給額（毎月固定で支払われる額の総額で、固定残業代と固定手当を含む）。"
            "sourceに総額があればそのまま入れる。総額がなく、その応募枠で全員に毎月固定で"
            "支払われる内訳（基本給、固定残業代・みなし残業代、調整手当や一律のリモート勤務手当などの"
            "固定手当）が示されていれば、その合計を入れ、内訳をnoteに書く。"
            "基本給しか示されていなければ null。"
        ),
        json_schema_extra={
            "x-do_not": (
                "通勤手当、住宅・家族手当、一部の人だけが受け取る地域手当など、個人の条件で"
                "変わる手当や実費精算を合計に加えない。一時金・賞与を加えない。"
                "基本給だけから合計を計算しない。"
            )
        },
    )
    base_salary_yen: MetricNumber | None = Field(
        default=None,
        description="sourceが基本給（基本月額・本給）として明示する月額。月給額の内訳。記載がなければ null。",
        json_schema_extra={
            "x-do_not": (
                "内訳のない月給額を基本給として入れない。月給額から固定残業代や手当を"
                "差し引いて計算しない。"
            )
        },
    )
    fixed_overtime_yen: MetricNumber | None = Field(
        default=None,
        description="月給額に含まれる固定残業代（みなし残業代）の月額。記載がなければ null。",
    )
    fixed_overtime_hours: MetricNumber | None = Field(
        default=None,
        description="固定残業代に対応する月の時間数。記載がなければ null。",
    )

    @model_validator(mode="after")
    def validate_has_salary_amount(self) -> StartingSalary:
        if self.monthly_total_yen is None and self.base_salary_yen is None:
            raise ValueError("starting salary requires monthly_total_yen or base_salary_yen")
        return self


_SLOT_NAME_DO_NOT = "source上で確認できない名称を作らない。"



class ApplicationSlot(SchemaModel):
    slot_name: str = Field(
        description="応募枠のsource上の名称。応募枠の分け方はapplication_slotsに従う。",
        examples=["全国勤務総合職", "地域限定職", "グループ会社別採用枠"],
        json_schema_extra={"x-do_not": _SLOT_NAME_DO_NOT},
    )
    application_year: int = Field(
        description=_APPLICATION_YEAR_DESCRIPTION,
        examples=[2028],
        json_schema_extra={"x-do_not": _APPLICATION_YEAR_DO_NOT},
    )
    application_windows: ApplicationWindows = Field(
        description=(
            "同じ応募枠に早期選考、通常選考、複数回締切がある場合も、開始日と締切日の"
            "対応を募集回ごとに保持するscope。"
        ),
        json_schema_extra={
            "x-do_not": "日程だけが異なる募集回を別のApplicationSlotとして重複作成しない。"
        },
    )
    assignment_companies: AssignmentCompanies
    starting_salary: SourcedMetric[StartingSalary] = Field(
        description="応募枠の学士月額初任給。" + _SALARY_DESCRIPTION_TAIL,
        json_schema_extra={"x-do_not": _SALARY_DO_NOT},
    )
    starting_salary_master: SourcedMetric[StartingSalary] = Field(
        description="応募枠の修士月額初任給。" + _SALARY_DESCRIPTION_TAIL,
        json_schema_extra={"x-do_not": _SALARY_DO_NOT},
    )
    starting_salary_doctor: SourcedMetric[StartingSalary] = Field(
        description="応募枠の博士月額初任給。" + _SALARY_DESCRIPTION_TAIL,
        json_schema_extra={"x-do_not": _SALARY_DO_NOT},
    )
    has_remote_work: SourcedMetric[bool] = Field(
        description=(
            "応募枠でリモート勤務制度があるかどうか。応募枠に固有の記述がない場合は、"
            "企業全体の制度を示すsource（非公式を含む）を根拠にしてよい。"
        ),
        json_schema_extra={"x-do_not": "別募集枠専用の勤務制度 source を流用しない。"},
    )
    selection_process: SelectionProcess = Field(
        description=(
            "応募枠の選考プロセスと各工程の有無をまとめる scope。各工程の有無は、"
            "応募枠の選考フローを示すsourceにその工程が含まれない場合、またはsource"
            "（非公式を含む）がその工程がないと明言する場合に false とする。"
        )
    )


class ApplicationSlotIdentityRecord(SchemaModel):
    application_year: int = Field(
        description=_APPLICATION_YEAR_DESCRIPTION,
        examples=[2028],
        json_schema_extra={"x-do_not": _APPLICATION_YEAR_DO_NOT},
    )
    slot_name: str = Field(
        description="応募枠のsource上の名称。応募枠の分け方はapplication_slotsに従う。",
        examples=["全国勤務総合職"],
        json_schema_extra={"x-do_not": _SLOT_NAME_DO_NOT},
    )
    source: Source = Field(
        description=(
            "このslot_nameの新卒本選考枠が存在することを直接支える情報源。"
        )
    )


class ApplicationSlotInventory(SchemaModel):
    records: list[ApplicationSlotIdentityRecord] = Field(
        description=(
            "sourceで確認できた応募枠のidentity一覧。"
            "identityはapplication_yearとslot_nameの組で定まる。"
            "対象年度と前年度を調査しても確認できなかった（Missing）場合は空配列にする。"
        ),
    )
    note: str | None = Field(default=None, description=NOTE_DESCRIPTION)

    @model_validator(mode="after")
    def validate_note(self) -> ApplicationSlotInventory:
        require_note_when_empty(self.records, self.note)
        return self


class RecruitmentCase(SchemaModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "recruitment_year": 2028,
                    "publishing_company_id": "1234567890123",
                    "application_slot_inventory": {
                        "records": [],
                        "note": "2028年度と2027年度の新卒本選考枠を確認できなかった。",
                    },
                    "application_slots": [],
                }
            ]
        },
    )

    recruitment_year: int = Field(
        description="今回の調査基準とする新卒採用の入社年。入力値を保持する。",
        examples=[2028],
    )
    publishing_company_id: CompanyId = Field(
        pattern=r"^[0-9]{13}$",
        description="採用情報を外部に公表している企業の法人番号。",
    )
    application_slot_inventory: ApplicationSlotInventory = Field(
        description=(
            "対象年度と前年度について、どの新卒本選考の応募枠が存在すると"
            "確認したかをsource付きで保持する探索結果の正本。"
        ),
        json_schema_extra={
            "x-do_not": (
                "application_slotsから名前を機械生成してsourceを後付けしない。"
                "詳細metricが見つかったことだけを根拠にidentityを追加しない。"
                "前年度のidentityを対象年度へ複製しない。"
            )
        },
    )
    application_slots: list[ApplicationSlot] = Field(
        description=(
            "新卒本選考の応募枠の一覧。応募枠は次のように分ける。"
            "応募（エントリー）が別の募集区分は別の応募枠にする。同じ応募でも、待遇、"
            "応募資格、勤務地・雇用範囲、選考工程、採用主体などの採用条件が独立して"
            "設定される募集区分は別の応募枠にする。国内選考と海外選考のように選考工程が"
            "異なる選考ルートも別の応募枠にする。同じ応募と採用条件の中で選ぶ希望職種、"
            "仕事内容、配属候補は、それらをまとめる名称（総合職など）がsourceにある場合"
            "だけ1つの応募枠にまとめ、ない場合はsource上の名称ごとに分ける。"
            "日程だけが異なる募集回はapplication_windowsで保持する。"
            "年度が異なる応募枠は別のApplicationSlotとして保持し、28 卒と 29 卒など"
            "複数年度の slot を同時に含めてよい。"
            "応募枠を対象とするsourceが参考として示す過去の実績（「前年度実績」"
            "「2026年4月実績」など）は、その応募枠の値として記録し、何年の実績かをnoteに書く。"
            "対象年度と前年度を調査しても実在する新卒本選考の"
            "応募枠を確認できなかった場合は空配列にする。成果物が存在しない"
            "調査未実施状態とは区別する。"
        ),
        json_schema_extra={
            "x-do_not": (
                "インターン、インターンシップ、仕事体験、オープンカンパニーなど、"
                "新卒本選考でない枠を含めない。別の年度の応募枠を対象とするsourceの値を使わない。"
                "枠名や詳細を確認できない募集予告からslotを推測で作らない。"
                "応募が別の募集区分を1つのslotにまとめない。"
            )
        },
    )

    @model_validator(mode="after")
    def validate_application_slot_inventory(self) -> RecruitmentCase:
        detail_identities = [
            (slot.application_year, slot.slot_name) for slot in self.application_slots
        ]

        duplicate_details = _duplicate_identities(detail_identities)
        if duplicate_details:
            raise ValueError(
                "application_slots contains duplicate identities: "
                f"{_format_identities(duplicate_details)}"
            )

        inventory_identities = [
            (record.application_year, record.slot_name)
            for record in self.application_slot_inventory.records
        ]
        duplicate_inventory = _duplicate_identities(inventory_identities)
        if duplicate_inventory:
            raise ValueError(
                "application_slot_inventory contains duplicate identities: "
                f"{_format_identities(duplicate_inventory)}"
            )

        inventory_set = set(inventory_identities)
        detail_set = set(detail_identities)
        if inventory_set != detail_set:
            missing_details = inventory_set - detail_set
            missing_inventory = detail_set - inventory_set
            parts = []
            if missing_details:
                parts.append(
                    "missing from application_slots: "
                    f"{_format_identities(missing_details)}"
                )
            if missing_inventory:
                parts.append(
                    "missing from application_slot_inventory: "
                    f"{_format_identities(missing_inventory)}"
                )
            raise ValueError(
                "application_slot_inventory and application_slots identity sets "
                f"must match; {'; '.join(parts)}"
            )

        return self


def _duplicate_identities(
    identities: list[tuple[int, str]],
) -> set[tuple[int, str]]:
    seen: set[tuple[int, str]] = set()
    duplicates: set[tuple[int, str]] = set()
    for identity in identities:
        if identity in seen:
            duplicates.add(identity)
        seen.add(identity)
    return duplicates


def _format_identities(identities: set[tuple[int, str]]) -> str:
    return ", ".join(
        f"({application_year}, {slot_name!r})"
        for application_year, slot_name in sorted(identities)
    )


