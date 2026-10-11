"""Pydantic models corresponding to common.schema.json.

Requires Pydantic v2.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, TypeVar
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


T = TypeVar("T")

CompanyId = str


class SchemaModel(BaseModel):
    """Base model mirroring `additionalProperties: false`."""

    model_config = ConfigDict(extra="forbid")


class SourceKind(StrEnum):
    OFFICIAL = "official"
    UNOFFICIAL = "unofficial"
    UNKNOWN = "unknown"


class Source(SchemaModel):
    kind: SourceKind = Field(
        description=(
            "情報源が公式か非公式か。掲載先サイトではなく、情報の発信・管理主体で判断する。"
            " official は次の2つを指す。(1) 企業本人、企業グループ、採用主体、IR 発信主体などが"
            "発信・管理する情報。就活サイト上の企業掲載ページや募集情報のように、掲載先が"
            "第三者でも企業本人が記載・管理していれば official とする。(2) 法令に基づく開示・"
            "届出・公表で、内容の正しさが法令で担保される情報（EDINET の有価証券報告書、"
            "官公庁が法令に基づいて公表するデータベースなど）。unofficial は第三者による編集、"
            "推定、口コミ、個人の体験談、報道、まとめ情報を指す。"
            "unknown は発信主体を確認できない場合に使う。"
        )
    )
    url: str = Field(description="参照した URL。")

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if not value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source url must be a non-empty HTTP or HTTPS URL")
        return value


def source_url_identity(url: str) -> str:
    """Evidence identity only; never rewrite a stored Source.url.

    Fragments, hostname case and default ports cannot create a second retrieval
    path for the same HTTP resource. Paths/query strings remain untouched.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    authority = f"[{host}]" if ":" in host else host
    port = parsed.port
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, ""))


NOTE_DESCRIPTION = (
    "読み手に必要な事情の自由記述。値がない（Missing、記録の一覧が空）場合は必須とし、"
    "非公開、該当なし、見つからない、項目の型へ正規化できない値（その値も書く）などを書く。"
    "値がある場合も、範囲や条件が付く、年度の記載がないsourceを使ったなど、値を誤解"
    "しうる事情があれば書く。対象年度の情報が未公開、前年度の情報しかないなど、年度に"
    "関する事情も書く。sourceの解釈や、source内の別箇所の記述を組み合わせた展開を"
    "行った場合は、その内容を書く。"
)


def require_note_when_empty(items, note) -> None:
    """Missing metrics must explain themselves in note."""
    if not items and not (note and note.strip()):
        raise ValueError("note is required when no records are found")


class MetricRecord(SchemaModel, Generic[T]):
    value: T = Field(
        description=(
            "見つかった値。source 上の値を、項目が定める単位、書式、列挙値へ正規化して入れる。"
            "万円、億円などの単位表記を項目の単位へ直すことは正規化に含む。"
        ),
        json_schema_extra={
            "x-do_not": (
                "要約、推測、類推で作った値や、月額と年額の換算、合算など source にない値を"
                "計算した値は入れない。"
            )
        },
    )
    source: Source = Field(description="value の直接根拠となる情報源。")


class SourcedMetric(SchemaModel, Generic[T]):
    records: list[MetricRecord[T]] = Field(
        description="値と直接根拠の一覧。値がない（Missing）場合は空配列にする。",
        json_schema_extra={"x-do_not": "同じ URL の source を根拠に異なる値を並べない。"},
    )
    note: str | None = Field(default=None, description=NOTE_DESCRIPTION)

    @model_validator(mode="after")
    def validate_note(self) -> SourcedMetric[T]:
        require_note_when_empty(self.records, self.note)
        return self

    @model_validator(mode="after")
    def validate_one_value_per_url(self) -> SourcedMetric[T]:
        value_by_url = {}
        conflicting_urls = set()
        for record in self.records:
            url = record.source.url
            if url in value_by_url and value_by_url[url] != record.value:
                conflicting_urls.add(url)
            value_by_url.setdefault(url, record.value)
        if conflicting_urls:
            raise ValueError(
                "records with the same source URL have different values: "
                + ", ".join(sorted(conflicting_urls))
            )
        return self
