"""
core/ingestion/parser.py — Parser abstraction + all modality parsers.

Each parser produces a list[ParsedChunk]. Everything downstream is modality-agnostic.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── Canonical schema ──────────────────────────────────────────────────────────

class SourceLocator(BaseModel):
    page: int | None = None
    time_range: tuple[float, float] | None = None
    cell_range: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class ParsedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    modality: Literal["text", "table", "image", "video_segment", "audio_segment"]
    content: str
    context_prefix: str | None = None
    source_locator: SourceLocator
    raw_file_uri: str
    parser_backend: str
    embedding_model: str = ""
    created_at: datetime = datetime.now(timezone.utc)


# ── Abstract Parser ───────────────────────────────────────────────────────────

class Parser(ABC):
    @abstractmethod
    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        ...


# ── Helpers ───────────────────────────────────────────────────────────────────

def compute_doc_id(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def _make_chunk(
    doc_id: str,
    index: int,
    content: str,
    modality: Literal["text", "table", "image", "video_segment", "audio_segment"],
    locator: SourceLocator,
    raw_file_uri: str,
    backend: str,
) -> ParsedChunk:
    chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{index}"))
    return ParsedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        modality=modality,
        content=content,
        source_locator=locator,
        raw_file_uri=raw_file_uri,
        parser_backend=backend,
    )


# ── PDF Parser ────────────────────────────────────────────────────────────────

class PdfParser(Parser):
    """PDF via Docling. Produces section-aware text chunks."""

    BACKEND = "docling-pdf"

    def __init__(self) -> None:
        from docling.document_converter import DocumentConverter
        self._converter = DocumentConverter()

    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        result = self._converter.convert(str(file_path))
        doc = result.document
        raw_file_uri = file_path.resolve().as_uri()
        chunks: list[ParsedChunk] = []

        top_section: str = ""
        current_section: str = ""
        current_lines: list[str] = []
        current_page: int | None = None
        block_idx = 0

        def _flush():
            nonlocal block_idx, current_lines, current_page
            if not current_lines:
                return
            if top_section and current_section and top_section != current_section:
                sec_path = f"{top_section} > {current_section}"
            else:
                sec_path = top_section or current_section
            prefix = f"[Section: {sec_path}]\n" if sec_path else ""
            chunks.append(_make_chunk(
                doc_id, block_idx,
                prefix + "\n".join(current_lines),
                "text",
                SourceLocator(page=current_page),
                raw_file_uri, self.BACKEND,
            ))
            block_idx += 1
            current_lines.clear()

        for item, _level in doc.iterate_items():
            item_type = type(item).__name__
            if item_type not in ("TextItem", "SectionHeaderItem", "ListItem", "TableItem"):
                continue
            text = item.text.strip() if hasattr(item, "text") else ""
            if not text and hasattr(item, "export_to_markdown"):
                text = item.export_to_markdown().strip()
            if not text:
                continue

            page_no: int | None = None
            if hasattr(item, "prov") and item.prov:
                prov = item.prov[0]
                if hasattr(prov, "page_no"):
                    page_no = prov.page_no
            if current_page is None:
                current_page = page_no

            if item_type == "SectionHeaderItem":
                _flush()
                if text.isupper() and len(text) <= 30:
                    top_section = text
                    current_section = text
                else:
                    current_section = text
                    current_lines.append(f"## {text}")
                current_page = page_no
            else:
                current_lines.append(text)
                if sum(len(l) for l in current_lines) >= 1500:
                    _flush()

        _flush()
        return chunks


# ── DOCX / PPTX Parser ───────────────────────────────────────────────────────

class DocxParser(Parser):
    """DOCX and PPTX via Docling — same converter, different file type."""

    BACKEND = "docling-docx"

    def __init__(self) -> None:
        from docling.document_converter import DocumentConverter
        self._converter = DocumentConverter()

    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        result = self._converter.convert(str(file_path))
        text = result.document.export_to_markdown()
        raw_file_uri = file_path.resolve().as_uri()
        return [_make_chunk(doc_id, 0, text, "text",
                            SourceLocator(), raw_file_uri, self.BACKEND)]


# ── XLSX / CSV Parser ─────────────────────────────────────────────────────────

class XlsxParser(Parser):
    """
    Spreadsheet parser. Reads each sheet and emits row-group chunks.
    Every chunk repeats the header row so context is never lost.
    """

    BACKEND = "pandas-xlsx"

    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        import pandas as pd
        from config import settings

        raw_file_uri = file_path.resolve().as_uri()
        ext = file_path.suffix.lower()
        chunks: list[ParsedChunk] = []
        chunk_idx = 0
        row_limit = settings.table_chunk_rows

        if ext == ".csv":
            sheets = {"Sheet1": pd.read_csv(file_path)}
        else:
            sheets = pd.read_excel(file_path, sheet_name=None)  # all sheets

        for sheet_name, df in sheets.items():
            df = df.dropna(how="all")
            if df.empty:
                continue
            header = df.columns.tolist()
            rows = df.values.tolist()

            for start in range(0, len(rows), row_limit):
                group = rows[start: start + row_limit]
                lines = ["\t".join(str(v) for v in header)]
                for row in group:
                    lines.append("\t".join(str(v) for v in row))
                content = f"[Sheet: {sheet_name} rows {start+1}-{start+len(group)}]\n" + "\n".join(lines)
                cell_range = f"{sheet_name}!A{start+1}:{len(header)}{start+len(group)}"
                chunks.append(_make_chunk(
                    doc_id, chunk_idx, content, "table",
                    SourceLocator(cell_range=cell_range),
                    raw_file_uri, self.BACKEND,
                ))
                chunk_idx += 1

        return chunks


# ── Image Parser ──────────────────────────────────────────────────────────────

class ImageParser(Parser):
    """
    Two-pass image understanding:
      1. BLIP-base  → visual scene caption (what the image looks like)
      2. RapidOCR   → printed/written text extraction (what text is in the image)
    Both signals are stored in the chunk so retrieval works on either.
    """

    BACKEND = "blip+rapidocr-image"

    def __init__(self) -> None:
        from transformers import BlipProcessor, BlipForConditionalGeneration
        from rapidocr_onnxruntime import RapidOCR
        import torch
        self._processor = BlipProcessor.from_pretrained(
            "Salesforce/blip-image-captioning-base"
        )
        self._model = BlipForConditionalGeneration.from_pretrained(
            "Salesforce/blip-image-captioning-base"
        ).to("cpu")
        self._torch = torch
        self._ocr = RapidOCR()

    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        import numpy as np
        from PIL import Image

        raw_file_uri = file_path.resolve().as_uri()
        image = Image.open(file_path).convert("RGB")

        # Pass 1: BLIP caption
        inputs = self._processor(images=image, return_tensors="pt")
        with self._torch.no_grad():
            out = self._model.generate(**inputs, max_new_tokens=64)
        caption = self._processor.decode(out[0], skip_special_tokens=True)

        # Pass 2: RapidOCR text extraction
        ocr_result, _ = self._ocr(np.array(image))
        ocr_lines = [item[1] for item in ocr_result if item[1].strip()] if ocr_result else []
        ocr_text = "\n".join(ocr_lines)

        # Combine: always include caption; append OCR text if found
        if ocr_text:
            content = (
                f"[Image: {file_path.name}]\n"
                f"Visual: {caption}\n"
                f"Text in image:\n{ocr_text}"
            )
        else:
            content = f"[Image: {file_path.name}]\n{caption}"

        return [_make_chunk(doc_id, 0, content, "image",
                            SourceLocator(), raw_file_uri, self.BACKEND)]



def _transcribe_audio(audio_path: str) -> list[tuple[float, float, str]]:
    """Transcribe audio/video using faster-whisper (CTranslate2).

    CPU-optimised settings:
      beam_size=1               greedy decoding, ~4-5x faster than beam=5
      vad_filter=True           skip silent segments before feeding to model
      condition_on_previous_text=False  avoids hallucination loops on noisy audio
    """
    from faster_whisper import WhisperModel
    from config import settings

    model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        audio_path,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    results: list[tuple[float, float, str]] = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            results.append((seg.start, seg.end, text))
    return results


def _merge_segments(
    segments: list[tuple[float, float, str]],
    max_words: int = 120,
    max_duration: float = 60.0,
) -> list[tuple[float, float, str]]:
    """
    Merge small adjacent timestamped audio/transcript segments into contextual windows
    (e.g., up to 120 words or 60 seconds of audio). Prevents 1-3 word micro-chunks.
    """
    if not segments:
        return []

    merged: list[tuple[float, float, str]] = []
    curr_start, curr_end, curr_texts = segments[0][0], segments[0][1], [segments[0][2]]
    curr_words = len(segments[0][2].split())

    for start, end, text in segments[1:]:
        words = len(text.split())
        duration = end - curr_start

        if (curr_words + words <= max_words) and (duration <= max_duration):
            curr_end = end
            curr_texts.append(text)
            curr_words += words
        else:
            merged.append((curr_start, curr_end, " ".join(curr_texts)))
            curr_start, curr_end, curr_texts = start, end, [text]
            curr_words = words

    if curr_texts:
        merged.append((curr_start, curr_end, " ".join(curr_texts)))

    return merged


# ── Video Parser ──────────────────────────────────────────────────────────────

class VideoParser(Parser):
    """
    Extracts audio from a video file and transcribes it with faster-whisper.
    Merges segments into ~60s/120-word contextual windows.
    """

    BACKEND = "whisper-video"

    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        raw_file_uri = file_path.resolve().as_uri()
        segments = _transcribe_audio(str(file_path))
        merged = _merge_segments(segments, max_words=120, max_duration=60.0)
        chunks: list[ParsedChunk] = []

        for i, (start, end, text) in enumerate(merged):
            content = f"[{file_path.name} {start:.1f}s–{end:.1f}s]\n{text}"
            chunks.append(_make_chunk(
                doc_id, i, content, "video_segment",
                SourceLocator(time_range=(start, end)),
                raw_file_uri, self.BACKEND,
            ))
        return chunks


# ── YouTube caption helpers ───────────────────────────────────────────────────

def _fetch_yt_captions(url: str, tmp_dir: Path) -> list[tuple[float, float, str]]:
    """Try to download YouTube auto-captions via yt-dlp. Returns segments or [].

    Prefers manual captions → auto-generated, English first.
    Caption download requires no JS runtime and completes in seconds.
    """
    import re
    import yt_dlp

    sub_path = tmp_dir / "captions"
    ydl_opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": ["en", "en-orig", "en-US"],
        "subtitlesformat": "vtt",
        "outtmpl": str(sub_path),
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # yt-dlp writes e.g. captions.en.vtt or captions.en-orig.vtt
    vtt_files = list(tmp_dir.glob("captions*.vtt"))
    if not vtt_files:
        return []

    vtt_text = vtt_files[0].read_text(encoding="utf-8", errors="ignore")
    return _parse_vtt(vtt_text)


def _parse_vtt(vtt: str) -> list[tuple[float, float, str]]:
    """Parse WebVTT into (start_sec, end_sec, text) tuples. Deduplicates overlapping cues."""
    import re

    # Match timestamp lines: 00:00:01.000 --> 00:00:04.000
    cue_re = re.compile(
        r"(\d+):(\d{2}):(\d{2})\.(\d{3})\s+-->\s+(\d+):(\d{2}):(\d{2})\.(\d{3})"
    )
    segments: list[tuple[float, float, str]] = []
    lines = vtt.splitlines()
    i = 0
    while i < len(lines):
        m = cue_re.match(lines[i])
        if m:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
            start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
            end   = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                # Strip VTT inline tags like <00:00:01.000><c>text</c>
                clean = re.sub(r"<[^>]+>", "", lines[i]).strip()
                if clean:
                    text_lines.append(clean)
                i += 1
            text = " ".join(text_lines)
            if text and (not segments or segments[-1][2] != text):
                segments.append((start, end, text))
        else:
            i += 1
    return segments


# ── YouTube Parser ────────────────────────────────────────────────────────────

class YouTubeParser(Parser):
    """
    Ingests a YouTube video as text chunks.

    Strategy (fast path first):
      1. Download auto-captions via yt-dlp (seconds, no inference).
      2. If no captions available, download audio + transcribe with faster-whisper.
      3. Merge segments into ~60s/120-word contextual windows before chunking.

    file_path.name carries the YouTube URL (passed as fake Path by the router).
    """

    BACKEND = "youtube"

    def parse(self, file_path: Path, doc_id: str) -> list[ParsedChunk]:
        import tempfile

        url = file_path.name  # URL embedded as fake path by the router

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Fast path: YouTube captions (no Whisper)
            segments = _fetch_yt_captions(url, tmp_path)
            source = "captions"

            if not segments:
                # Fallback: download audio + Whisper transcription
                logger.info("No captions found for %s — falling back to Whisper", url)
                audio_path = tmp_path / "audio.mp3"
                _download_yt_audio(url, audio_path)
                segments = _transcribe_audio(str(audio_path))
                source = "whisper"

        logger.info("YouTube %s: %d raw segments via %s", url, len(segments), source)
        merged = _merge_segments(segments, max_words=120, max_duration=60.0)
        logger.info("YouTube %s: merged into %d contextual chunks", url, len(merged))

        chunks: list[ParsedChunk] = []
        for i, (start, end, text) in enumerate(merged):
            content = f"[YouTube {start:.1f}s–{end:.1f}s]\n{text}"
            chunks.append(_make_chunk(
                doc_id, i, content, "audio_segment",
                SourceLocator(time_range=(start, end)),
                url, self.BACKEND,
            ))
        return chunks


def _download_yt_audio(url: str, audio_path: Path) -> None:
    """Download best audio from a YouTube URL to audio_path (mp3)."""
    import yt_dlp
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(audio_path.with_suffix("")),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])



# ── Factory ───────────────────────────────────────────────────────────────────

_EXTENSION_MAP: dict[str, type[Parser]] = {
    "pdf":  PdfParser,
    "docx": DocxParser,
    "pptx": DocxParser,
    "xlsx": XlsxParser,
    "csv":  XlsxParser,
    "jpg":  ImageParser,
    "jpeg": ImageParser,
    "png":  ImageParser,
    "webp": ImageParser,
    "gif":  ImageParser,
    "mp4":  VideoParser,
    "mov":  VideoParser,
    "avi":  VideoParser,
    "webm": VideoParser,
    "youtube": YouTubeParser,
}


def get_parser(modality: str) -> Parser:
    cls = _EXTENSION_MAP.get(modality.lower())
    if cls is None:
        raise ValueError(
            f"Unsupported modality '{modality}'. Supported: {list(_EXTENSION_MAP)}"
        )
    return cls()
