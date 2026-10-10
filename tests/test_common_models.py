import unittest

from pydantic import ValidationError

from schemas.common_models import Source, SourcedMetric


class CommonModelsTests(unittest.TestCase):
    def test_empty_records_require_free_text_note(self):
        metric = SourcedMetric[int].model_validate(
            {"records": [], "note": "年俸制のみ記載：710万円"}
        )
        self.assertEqual(metric.note, "年俸制のみ記載：710万円")

        for payload in (
            {"records": []},
            {"records": [], "note": "  "},
            {"records": [], "note": "x", "missing_reason": "x"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    SourcedMetric[int].model_validate(payload)

    def test_found_records_may_carry_note(self):
        record = {"value": 1, "source": {"kind": "official", "url": "https://example.com"}}
        self.assertIsNone(SourcedMetric[int].model_validate({"records": [record]}).note)
        metric = SourcedMetric[int].model_validate(
            {"records": [record], "note": "年度の記載がないsource。"}
        )
        self.assertEqual(metric.note, "年度の記載がないsource。")

    def test_same_source_url_requires_same_value(self):
        def record(value, url):
            return {"value": value, "source": {"kind": "official", "url": url}}

        SourcedMetric[bool].model_validate(
            {
                "records": [
                    record(True, "https://example.com/a"),
                    record(True, "https://example.com/a"),
                    record(False, "https://example.com/b"),
                ]
            }
        )
        with self.assertRaisesRegex(ValidationError, "https://example.com/a"):
            SourcedMetric[bool].model_validate(
                {
                    "records": [
                        record(True, "https://example.com/a"),
                        record(False, "https://example.com/a"),
                    ]
                }
            )

    def test_source_url_is_trimmed_and_accepts_http_or_https(self):
        for url in (
            "http://example.com/source",
            " https://example.com/source?q=1 ",
        ):
            with self.subTest(url=url):
                source = Source.model_validate({"kind": "official", "url": url})
                self.assertEqual(source.url, url.strip())

    def test_source_url_rejects_empty_or_non_http_values(self):
        for url in ("", "   ", "example.com/source", "ftp://example.com/source"):
            with self.subTest(url=url):
                with self.assertRaises(ValidationError):
                    Source.model_validate({"kind": "official", "url": url})


if __name__ == "__main__":
    unittest.main()
