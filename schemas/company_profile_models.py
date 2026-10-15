"""Pydantic models corresponding to company_profile.schema.json."""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator

from .common_models import CompanyId, SchemaModel, SourcedMetric


MetricNumber = float


class CompanyKind(StrEnum):
    LEGAL_ENTITY = "legal_entity"
    UNKNOWN = "unknown"


class RemoteWorkPolicy(StrEnum):
    FULL = "full"
    HYBRID = "hybrid"
    LIMITED = "limited"
    NONE = "none"


class EruboshiCertificationTier(StrEnum):
    STAGE_1 = "stage_1"
    STAGE_2 = "stage_2"
    STAGE_3 = "stage_3"
    PLATINUM = "platinum"


class IndustryClassificationCode(StrEnum):
    A_AGRICULTURE_FORESTRY = "A_agriculture_forestry"
    B_FISHERY = "B_fishery"
    C_MINING_QUARRYING_GRAVEL = "C_mining_quarrying_gravel"
    D_CONSTRUCTION = "D_construction"
    E_MANUFACTURING = "E_manufacturing"
    F_ELECTRICITY_GAS_HEAT_WATER = "F_electricity_gas_heat_water"
    G_INFORMATION_COMMUNICATIONS = "G_information_communications"
    H_TRANSPORT_POSTAL = "H_transport_postal"
    I_WHOLESALE_RETAIL = "I_wholesale_retail"
    J_FINANCE_INSURANCE = "J_finance_insurance"
    K_REAL_ESTATE_LEASING = "K_real_estate_leasing"
    L_SCIENTIFIC_RESEARCH_PROFESSIONAL_TECHNICAL_SERVICES = (
        "L_scientific_research_professional_technical_services"
    )
    M_ACCOMMODATION_FOOD_SERVICES = "M_accommodation_food_services"
    N_LIVING_RELATED_PERSONAL_SERVICES_ENTERTAINMENT = (
        "N_living_related_personal_services_entertainment"
    )
    O_EDUCATION_LEARNING_SUPPORT = "O_education_learning_support"
    P_MEDICAL_HEALTHCARE_WELFARE = "P_medical_healthcare_welfare"
    Q_COMPOUND_SERVICES = "Q_compound_services"
    R_SERVICES_N_E_C = "R_services_n_e_c"
    S_GOVERNMENT = "S_government"
    T_UNCLASSIFIABLE = "T_unclassifiable"


class BasicInformation(SchemaModel):
    average_annual_income_yen: SourcedMetric[MetricNumber] = Field(
        description="年額の平均年収。",
        json_schema_extra={"x-do_not": "初任給、想定年収、求人票上の参考年収は入れない。"},
    )
    new_graduate_turnover_rate_within_3_years_percent: SourcedMetric[MetricNumber] = Field(
        description="新卒入社者集団の3年以内離職率。",
        json_schema_extra={"x-do_not": "全社平均離職率や推定値は入れない。"},
    )
    average_tenure_years: SourcedMetric[MetricNumber] = Field(description="平均勤続年数。")
    average_age_years: SourcedMetric[MetricNumber] = Field(description="平均年齢。")
    average_overtime_hours_per_month: SourcedMetric[MetricNumber] = Field(
        description="月平均残業時間。",
        json_schema_extra={"x-do_not": "みなし残業や固定残業時間は入れない。"},
    )
    annual_holidays_days: SourcedMetric[MetricNumber] = Field(
        description="明示された年間休日数。",
        json_schema_extra={"x-do_not": "週休や休暇制度から自分で合算しない。"},
    )
    paid_leave_usage_days: SourcedMetric[MetricNumber] = Field(description="有給休暇の平均取得日数。")
    remote_work_policy: SourcedMetric[RemoteWorkPolicy] = Field(
        description=(
            "リモート勤務方針。full は原則フルリモート、hybrid は併用が制度として明示、"
            "limited は一部条件付きでのみ明示、none はリモート制度なしまたは原則出社が"
            "明示されている状態を指す。"
        )
    )
    eruboshi_certification_tier: SourcedMetric[EruboshiCertificationTier] = Field(
        description=(
            "女性活躍推進法に基づくえるぼし認定の段階。stage_1 から stage_3 は"
            "えるぼし認定の各段階、platinum はプラチナえるぼし認定を指す。"
        ),
        json_schema_extra={
            "x-do_not": (
                "個別の採用枠における女性の積極募集、女性優遇、その他の採用条件を示す値"
                "として扱わない。女性向け制度、女性比率、受賞歴を認定段階の代用にしない。"
                "認定の直接根拠がない場合は段階を推測せず Missing にし、"
                "未確認を非認定として扱わない。"
            )
        },
    )
    established_year: SourcedMetric[MetricNumber] = Field(description="設立年。")
    employee_count: SourcedMetric[MetricNumber] = Field(
        description="従業員数。対象法人単体の値を優先し、単体の値が見つからない場合だけ連結などの値を記録する。",
        json_schema_extra={"x-do_not": "単体の値を確認できるのに連結などの値を記録しない。"},
    )
    capital_yen: SourcedMetric[MetricNumber] = Field(description="資本金。")
    research_and_development_expenses_yen: SourcedMetric[MetricNumber] = Field(
        description="企業の研究開発費。",
        json_schema_extra={"x-do_not": "設備投資、販管費、研究開発費以外の費用を入れない。"},
    )
    research_output_publication_presence: SourcedMetric[bool] = Field(
        description=(
            "企業公式 source で、論文、特許、技術報告、研究成果記事などの研究アウトプット"
            "公開が確認できるか。"
        ),
        json_schema_extra={
            "x-do_not": "単発ニュース、採用広報、非公式まとめだけで true にしない。曖昧な場合は false にせず Missing にする。"
        },
    )
    industry_academia_collaboration_presence: SourcedMetric[bool] = Field(
        description="企業公式 source で、大学・研究機関との共同研究、産学連携、学術連携が確認できるか。",
        json_schema_extra={
            "x-do_not": "一般的な業務提携、販売提携、顧客案件だけで true にしない。曖昧な場合は false にせず Missing にする。"
        },
    )
    industry_classification_codes: SourcedMetric[list[IndustryClassificationCode]] = Field(
        description=(
            "対象法人が営む事業を、日本標準産業分類の大分類のコードで記録する（1〜5個、重複なし）。"
            "有価証券報告書、統合報告書、会社概要などで事業セグメント（なければ事業内容）を確認し、"
            "各事業を大分類の定義に当てはめる。複数の事業を含むセグメントは主な事業で当てはめる。"
            "sourceが大分類そのものを示している必要はない。"
            "確認できた大分類はすべて記録し、同じ大分類の事業は合算する。"
            "売上高が分かる大分類は売上高の大きい順に並べ、売上高が分からない大分類はその後に"
            "主要なものから並べる。"
            "外資の日本法人などは、その法人自身の事業で判定する。持株会社など対象法人自体が"
            "事業を営まない場合だけ、グループの連結セグメントで判定する。"
            "この指標は判定結果なので、sourceの種類にかかわらずrecordは1件にまとめ、根拠"
            "（セグメント名や事業内容、売上高、当てはめた大分類、使ったsource）をnoteに書く。"
            "事業内容を確認できない場合は、確認に使ったsourceで T_unclassifiable を記録する。"
        ),
        json_schema_extra={
            "x-do_not": (
                "採用職種、研究テーマ、部署名、製品カテゴリだけから分類しない。"
                "第三者サイトの業種表示だけで決めない。"
                "売上高や順序が分からないことを理由に、確認できた大分類を省かない。"
                "持株会社以外で、親会社やグループの事業で対象法人の事業を置き換えない。"
                "Missingにしない。"
            )
        },
    )

    @field_validator("industry_classification_codes")
    @classmethod
    def validate_industry_codes(cls, metric):
        for record in metric.records:
            codes = record.value
            if not 1 <= len(codes) <= 5:
                raise ValueError("industry_classification_codes must have 1 to 5 codes")
            if len(set(codes)) != len(codes):
                raise ValueError("industry_classification_codes must not contain duplicates")
        return metric


class CompanyProfile(SchemaModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "company_id": "1234567890123",
                    "legal_name": "サンプル株式会社",
                    "company_kind": "legal_entity",
                }
            ]
        },
    )

    company_id: CompanyId = Field(
        pattern=r"^[0-9]{13}$",
        description="この企業プロフィールの法人番号。ファイル名 <company_id>.json と一致させる。",
        json_schema_extra={"x-do_not": "同じ企業に複数の company_id を作らない。"},
    )
    legal_name: str = Field(
        description="正式法人名。登記上または公式に確認できる名称を記録する。",
        examples=["サンプル株式会社"],
        json_schema_extra={"x-do_not": "略称、通称、部門名、ブランド名だけで置き換えない。"},
    )
    company_kind: CompanyKind = Field(
        description="対象種別。通常は legal_entity を使う。",
        json_schema_extra={"x-do_not": "法人でないブランド名、部署名、研究所名、企業グループ名を company_profile として作らない。"},
    )
    basic_information: BasicInformation = Field(
        description=(
            "平均年収、勤続年数、残業、休日、企業規模など、企業単位で比較する基本情報。"
            "1つのsourceに時点の異なる値が並ぶ場合は、最新時点の値を記録する。"
        ),
        json_schema_extra={"x-do_not": "採用枠、応募枠、募集年度に依存する情報はここに書かない。"},
    )
