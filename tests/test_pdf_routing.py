"""PDF routing tests that do not load Docling or OCR models."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.ingestion.parser import PdfParser
from core.ingestion.pdf_routing import (
    _text_metrics,
    _usable_native_text,
    parse_pdf_fast,
)


def _settings(**overrides):
    values = {
        "pdf_native_min_chars": 80,
        "pdf_docling_fallback_page_ratio": 0.70,
        "pdf_ocr_render_dpi": 200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _native_page(page: int, text: str = "A trustworthy native PDF sentence. " * 6):
    return {"page": page, "native_text": text, "route": "native"}


class PdfRoutingTests(unittest.TestCase):
    def test_native_quality_rejects_short_or_corrupt_text(self) -> None:
        self.assertTrue(_usable_native_text(_text_metrics("Native text with words. " * 8), 80))
        self.assertFalse(_usable_native_text(_text_metrics("Page 1"), 80))
        self.assertFalse(_usable_native_text(_text_metrics("\ufffd" * 100), 80))

    @patch("core.ingestion.pdf_routing._preflight_pdf")
    def test_native_document_does_not_construct_ocr_or_docling(self, preflight) -> None:
        preflight.return_value = [_native_page(1), _native_page(2)]
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            chunks, reason = parse_pdf_fast(Path(source.name), "doc-1", _settings())

        self.assertIsNone(reason)
        self.assertEqual([chunk.source_locator.page for chunk in chunks], [1, 2])
        self.assertTrue(all(chunk.parser_backend == "pdfium-native" for chunk in chunks))

    @patch("core.ingestion.pdf_routing._ocr_candidate_pages")
    @patch("core.ingestion.pdf_routing._preflight_pdf")
    def test_mixed_document_ocrs_only_candidate_pages(self, preflight, ocr_pages) -> None:
        pages = [
            _native_page(1),
            {"page": 2, "native_text": "", "route": "ocr"},
        ]
        preflight.return_value = pages
        ocr_pages.side_effect = lambda _path, plans, _dpi: plans[0].update(
            ocr_text="Scanned page with enough readable OCR content for ingestion."
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            chunks, reason = parse_pdf_fast(Path(source.name), "doc-2", _settings())

        self.assertIsNone(reason)
        self.assertEqual([chunk.representation for chunk in chunks], ["text", "ocr"])
        self.assertEqual(ocr_pages.call_args.args[1], [pages[1]])

    @patch("core.ingestion.pdf_routing._ocr_candidate_pages")
    @patch("core.ingestion.pdf_routing._preflight_pdf")
    def test_visual_only_page_does_not_force_full_document_fallback(
        self, preflight, ocr_pages
    ) -> None:
        pages = [
            _native_page(1),
            {"page": 2, "native_text": "", "route": "ocr"},
        ]
        preflight.return_value = pages
        ocr_pages.side_effect = lambda _path, plans, _dpi: plans[0].update(ocr_text="")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            chunks, reason = parse_pdf_fast(Path(source.name), "doc-visual", _settings())

        self.assertIsNone(reason)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(pages[1]["route"], "visual_only")

    @patch("core.ingestion.pdf_routing._ocr_candidate_pages")
    @patch("core.ingestion.pdf_routing._preflight_pdf")
    def test_low_native_coverage_selects_docling_before_ocr(self, preflight, ocr_pages) -> None:
        preflight.return_value = [
            _native_page(1),
            {"page": 2, "native_text": "", "route": "ocr"},
            {"page": 3, "native_text": "", "route": "ocr"},
            {"page": 4, "native_text": "", "route": "ocr"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            chunks, reason = parse_pdf_fast(Path(source.name), "doc-3", _settings())

        self.assertIsNone(chunks)
        self.assertEqual(reason, "low_native_coverage")
        ocr_pages.assert_not_called()

    @patch("core.ingestion.pdf_routing._preflight_pdf")
    def test_native_evidence_ids_are_stable_across_retries(self, preflight) -> None:
        preflight.return_value = [_native_page(1)]
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            path = Path(source.name)
            first, _ = parse_pdf_fast(path, "doc-4", _settings())
            second, _ = parse_pdf_fast(path, "doc-4", _settings())

        self.assertEqual(first[0].evidence_id, second[0].evidence_id)

    @patch("core.ingestion.pdf_routing.parse_pdf_fast")
    def test_pdf_parser_keeps_docling_lazy_on_native_path(self, fast_parse) -> None:
        fast_parse.return_value = ([], None)
        parser = PdfParser()

        result = parser.parse(Path("unused.pdf"), "doc-5")

        self.assertEqual(result, [])
        self.assertIsNone(parser._converter)


if __name__ == "__main__":
    unittest.main()
