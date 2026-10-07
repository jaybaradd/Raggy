"""Low-cost PDF page routing before the heavier Docling fallback.

PDFium supplies both embedded text and page rendering. Pages with trustworthy
embedded text avoid rendering entirely; only nonblank, low-text pages reach
RapidOCR. Returning ``None`` tells :class:`PdfParser` to use Docling.
"""

import logging
from pathlib import Path
from typing import Any

from core.latency import elapsed_ms, latency_span, log_latency, now_ns

logger = logging.getLogger(__name__)

_PREVIEW_DPI = 96
_MIN_WORDS = 15
_MIN_ALNUM_RATIO = 0.50
_MAX_BAD_CHAR_RATIO = 0.01
_MIN_VISUAL_CONTENT_RATIO = 0.005
_MIN_OCR_CONFIDENCE = 0.60


def parse_pdf_fast(path: Path, doc_id: str, settings: Any):
    """Return ``(chunks, None)`` or ``(None, reason)`` for Docling fallback."""
    common = {"doc_id": doc_id, "modality": "pdf"}
    try:
        with latency_span("ingestion", "pdf_preflight", **common) as fields:
            pages = _preflight_pdf(path, settings.pdf_native_min_chars, common)
            fields.update(_route_counts(pages))
    except Exception as exc:
        logger.warning("PDFium preflight failed for %s: %s", path.name, exc)
        return None, "preflight_error"

    nonblank = [page for page in pages if page["route"] != "blank"]
    uncertain = [page for page in pages if page["route"] == "uncertain"]
    ocr_pages = [page for page in pages if page["route"] == "ocr"]
    fallback_ratio = (len(ocr_pages) + len(uncertain)) / max(len(nonblank), 1)

    if uncertain:
        _log_route(common, pages, "docling", "uncertain_page")
        return None, "uncertain_page"
    if nonblank and fallback_ratio >= settings.pdf_docling_fallback_page_ratio:
        _log_route(common, pages, "docling", "low_native_coverage")
        return None, "low_native_coverage"

    if ocr_pages:
        try:
            with latency_span(
                "ingestion", "pdf_ocr", **common, page_count=len(ocr_pages)
            ) as fields:
                _ocr_candidate_pages(path, ocr_pages, settings.pdf_ocr_render_dpi)
                fields["output_chars"] = sum(len(page.get("ocr_text", "")) for page in ocr_pages)
        except Exception as exc:
            logger.warning("Selective PDF OCR failed for %s: %s", path.name, exc)
            _log_route(common, pages, "docling", "ocr_error")
            return None, "ocr_error"

    # A minority image page among otherwise native pages is usually a diagram or
    # photo. Docling currently ignores picture-only items too, so do not make one
    # such page force an expensive full-document reparse when OCR finds no text.
    for page in pages:
        if page["route"] == "ocr" and not _usable_ocr(page.get("ocr_text", "")):
            page["route"] = "visual_only"

    with latency_span("ingestion", "pdf_native_extract", **common) as fields:
        chunks = _pages_to_chunks(path, doc_id, pages)
        fields["raw_block_count"] = len(chunks)
        fields["output_chars"] = sum(len(chunk.content) for chunk in chunks)
    expected_pages = sum(page["route"] in {"native", "ocr"} for page in pages)
    represented_pages = len({chunk.source_locator.page for chunk in chunks})
    output_chars = sum(len(chunk.content) for chunk in chunks)
    with latency_span("ingestion", "pdf_quality_gate", **common) as fields:
        fields.update(
            expected_pages=expected_pages,
            represented_pages=represented_pages,
            output_chars=output_chars,
        )
        quality_ok = represented_pages == expected_pages and (
            not expected_pages or output_chars >= 40
        )
    if not quality_ok:
        _log_route(common, pages, "docling", "post_extract_quality_failure")
        return None, "post_extract_quality_failure"

    _log_route(common, pages, "mixed" if ocr_pages else "native", None)
    return chunks, None


def _preflight_pdf(
    path: Path,
    min_chars: int,
    log_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    pages: list[dict[str, Any]] = []
    preview_duration_ms = 0.0
    preview_count = 0
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_range()
                finally:
                    text_page.close()

                metrics = _text_metrics(text)
                if _usable_native_text(metrics, min_chars):
                    route = "native"
                    visual_ratio = None
                else:
                    preview_started = now_ns()
                    visual_ratio = _visual_content_ratio(page, _PREVIEW_DPI)
                    preview_duration_ms += elapsed_ms(preview_started)
                    preview_count += 1
                    route = "blank" if visual_ratio < _MIN_VISUAL_CONTENT_RATIO else "ocr"
                pages.append({
                    "page": index + 1,
                    "native_text": text,
                    "route": route,
                    "visual_content_ratio": visual_ratio,
                    **metrics,
                })
            except Exception:
                logger.exception("PDF preflight failed on page %d", index + 1)
                pages.append({"page": index + 1, "native_text": "", "route": "uncertain"})
            finally:
                page.close()
    finally:
        document.close()
    if preview_count:
        log_latency(
            "ingestion", "pdf_preview_render", preview_duration_ms,
            **(log_fields or {}), page_count=preview_count,
        )
    return pages


def _text_metrics(text: str) -> dict[str, float | int]:
    visible = [char for char in text if not char.isspace()]
    visible_count = len(visible)
    words = text.split()
    alnum_count = sum(char.isalnum() for char in visible)
    bad_count = sum((ord(char) < 32 and char not in "\n\r\t") or char == "\ufffd" for char in text)
    return {
        "char_count": visible_count,
        "word_count": len(words),
        "alnum_ratio": alnum_count / max(visible_count, 1),
        "bad_char_ratio": bad_count / max(len(text), 1),
    }


def _usable_native_text(metrics: dict[str, float | int], min_chars: int) -> bool:
    enough_text = metrics["char_count"] >= min_chars or metrics["word_count"] >= _MIN_WORDS
    return bool(
        enough_text
        and metrics["alnum_ratio"] >= _MIN_ALNUM_RATIO
        and metrics["bad_char_ratio"] <= _MAX_BAD_CHAR_RATIO
    )


def _visual_content_ratio(page: Any, dpi: int) -> float:
    import numpy as np

    bitmap = page.render(scale=dpi / 72)
    try:
        image = bitmap.to_pil().convert("L")
        pixels = np.asarray(image, dtype=np.int16)
    finally:
        bitmap.close()
    if not pixels.size:
        return 0.0
    border = np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]))
    background = float(np.median(border))
    return float(np.mean(np.abs(pixels - background) > 18))


def _ocr_candidate_pages(path: Path, pages: list[dict[str, Any]], dpi: int) -> None:
    import numpy as np
    import pypdfium2 as pdfium
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    document = pdfium.PdfDocument(str(path))
    try:
        for plan in pages:
            page = document[plan["page"] - 1]
            try:
                bitmap = page.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil().convert("RGB")
                    result, _ = ocr(np.asarray(image))
                finally:
                    bitmap.close()
            finally:
                page.close()
            accepted = [
                item[1].strip() for item in result or []
                if len(item) >= 3 and item[1].strip() and float(item[2]) >= _MIN_OCR_CONFIDENCE
            ]
            plan["ocr_text"] = "\n".join(accepted)
    finally:
        document.close()


def _usable_ocr(text: str) -> bool:
    metrics = _text_metrics(text)
    return bool(metrics["char_count"] >= 20 and metrics["alnum_ratio"] >= 0.40)


def _pages_to_chunks(path: Path, doc_id: str, pages: list[dict[str, Any]]):
    from core.ingestion.parser import SourceLocator, _make_chunk

    raw_file_uri = path.resolve().as_uri()
    chunks = []
    for plan in pages:
        route = plan["route"]
        if route not in {"native", "ocr"}:
            continue
        content = plan["native_text"] if route == "native" else plan.get("ocr_text", "")
        representation = "text" if route == "native" else "ocr"
        backend = "pdfium-native" if route == "native" else "rapidocr-pdf"
        chunks.append(_make_chunk(
            doc_id,
            plan["page"] - 1,
            content.strip(),
            "text",
            SourceLocator(page=plan["page"]),
            raw_file_uri,
            backend,
            representation=representation,
            stable_key=f"pdf:{plan['page']}:{representation}:0",
        ))
    return chunks


def _route_counts(pages: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "pages_total": len(pages),
        "pages_native": sum(page["route"] == "native" for page in pages),
        "pages_ocr": sum(page["route"] == "ocr" for page in pages),
        "pages_blank": sum(page["route"] == "blank" for page in pages),
        "pages_visual_only": sum(page["route"] == "visual_only" for page in pages),
        "pages_uncertain": sum(page["route"] == "uncertain" for page in pages),
    }


def _log_route(common: dict[str, Any], pages: list[dict[str, Any]], strategy: str,
               reason: str | None) -> None:
    log_latency(
        "ingestion",
        "pdf_route_selected",
        0,
        **common,
        **_route_counts(pages),
        selected_strategy=strategy,
        fallback_reason=reason,
    )
