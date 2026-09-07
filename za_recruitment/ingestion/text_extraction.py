# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Decide how to extract text from a submitted CV file, and do it.

Two strategies today:

- PDF: first check whether the document actually has a text layer (a native
  PDF) or is effectively a scanned image (near-zero raw extractable text)
  *before* deciding whether OCR is even needed, per the explicit design
  requirement. The text-layer path routes through
  `za_recruitment.ai.prompt_injection.extract_visible_text` so hidden/
  invisible spans never reach anything downstream; the OCR path has no
  equivalent visibility filter -- OCR output is inherently "what a human
  would see" because it comes from a rendered image, but an adversarial
  image crafted to fool OCR itself is a separate, unsolved problem (see
  `prompt_injection.py`'s docstring).
- DOCX: via python-docx, skipping runs marked hidden (`run.font.hidden`) --
  the DOCX equivalent of a PDF invisible span.

Legacy `.doc` is not handled -- no system dependency is available in this
environment to parse the old binary format -- returns
`method=METHOD_UNSUPPORTED`, `text=""`. The caller decides what that means
for the ingestion record (still stored, just not text-searchable/AI-ready).
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

import docx
import fitz  # PyMuPDF
import pytesseract
from PIL import Image

from za_recruitment.ai.prompt_injection import extract_visible_text

# Below this many raw (unfiltered) characters of extractable text across the
# whole document, a PDF is treated as scanned/image-based rather than a
# native text PDF, and routed to OCR instead.
MIN_RAW_TEXT_CHARS_FOR_TEXT_LAYER = 40

# A page whose largest embedded image covers at least this fraction of the
# page area is treated as needing OCR too, *even if* it also carries enough
# native text to cross MIN_RAW_TEXT_CHARS_FOR_TEXT_LAYER on its own -- a real
# CV confirmed this exact shape: page 1 had only a small native-text sliver
# (an address block, ~60 chars) sitting on top of a full-page embedded image
# containing the entire actual CV (heading, personal-info table, phone
# number). Checking native text length alone let that image go completely
# unread. Native + OCR text are merged for such a page rather than treated
# as mutually exclusive.
LARGE_IMAGE_AREA_RATIO = 0.15

OCR_RENDER_DPI = 200

METHOD_TEXT_LAYER = "Text Layer"
METHOD_OCR = "OCR"
METHOD_MIXED = "Mixed"
METHOD_DOCX = "DOCX"
METHOD_UNSUPPORTED = "Unsupported"


@dataclass
class ExtractionResult:
	text: str
	method: str
	page_count: int = 0
	stripped_span_count: int = 0
	stripped_samples: list[str] = field(default_factory=list)
	# 0-indexed page -> that page's own extracted text, populated on every
	# path (Text Layer / OCR / Mixed) so callers like
	# za_recruitment.ingestion.attachment_classifier always get a uniform
	# per-page view regardless of which extraction route this particular CV
	# took. DOCX has no real page concept, so it's a single {0: full_text}
	# entry -- bundled ID/certificate scans inside a DOCX are rare enough to
	# be out of scope for per-page classification in this pass.
	page_texts: dict[int, str] = field(default_factory=dict)
	# 0-indexed page -> METHOD_TEXT_LAYER or METHOD_OCR for that specific
	# page (only those two values -- a Mixed document has no DOCX/Unsupported
	# pages by definition). Lets attachment_classifier tell "just more typed
	# CV text" apart from "a scanned document of an unrecognised kind".
	page_methods: dict[int, str] = field(default_factory=dict)


def extract_cv_text(file_bytes: bytes, filename: str) -> ExtractionResult:
	extension = os.path.splitext(filename or "")[1].lower()

	if extension == ".pdf":
		return _extract_pdf(file_bytes)
	if extension == ".docx":
		return _extract_docx(file_bytes)

	return ExtractionResult(text="", method=METHOD_UNSUPPORTED)


def _extract_pdf(file_bytes: bytes) -> ExtractionResult:
	"""The text-vs-image decision is made *per page*, not once for the whole
	document -- a very common real CV shape is a typed CV page followed by
	scanned photocopies of an ID/certificates stapled behind it, which have
	no text layer at all. Deciding once for the whole document based on
	total text would let a single native-text page make the rest of the
	document's raw text length "big enough", silently skipping every
	scanned page instead of OCR'ing it.

	A page needs OCR if it lacks enough native text *or* if it carries a
	large embedded image regardless of native text length (see
	LARGE_IMAGE_AREA_RATIO) -- a page can legitimately have both a little
	native text and a large image that native extraction alone would never
	see, and such a page gets both sources merged rather than picking one.
	"""
	with fitz.open(stream=file_bytes, filetype="pdf") as doc:
		page_count = doc.page_count
		raw_lengths = [len(page.get_text()) for page in doc]
		has_large_image = [_page_has_large_image(page) for page in doc]

	has_native_indices = {i for i, length in enumerate(raw_lengths) if length >= MIN_RAW_TEXT_CHARS_FOR_TEXT_LAYER}
	needs_ocr_indices = {
		i for i in range(page_count) if raw_lengths[i] < MIN_RAW_TEXT_CHARS_FOR_TEXT_LAYER or has_large_image[i]
	}

	if not needs_ocr_indices:
		visible = extract_visible_text(file_bytes)
		return ExtractionResult(
			text=visible.text,
			method=METHOD_TEXT_LAYER,
			page_count=page_count,
			stripped_span_count=visible.stripped_span_count,
			stripped_samples=visible.stripped_samples,
			page_texts=visible.page_texts,
			page_methods=dict.fromkeys(visible.page_texts, METHOD_TEXT_LAYER),
		)

	if not has_native_indices:
		with fitz.open(stream=file_bytes, filetype="pdf") as doc:
			page_texts = _ocr_pdf_pages(doc)
		ocr_text = "\n".join(page_texts[i] for i in sorted(page_texts))
		return ExtractionResult(
			text=ocr_text,
			method=METHOD_OCR,
			page_count=page_count,
			page_texts=page_texts,
			page_methods=dict.fromkeys(page_texts, METHOD_OCR),
		)

	return _extract_mixed_pdf(file_bytes, page_count, needs_ocr_indices, has_native_indices)


def _page_has_large_image(page: "fitz.Page") -> bool:
	page_area = page.rect.width * page.rect.height
	if not page_area:
		return False

	for image in page.get_images():
		xref = image[0]
		for rect in page.get_image_rects(xref):
			if (rect.width * rect.height) / page_area >= LARGE_IMAGE_AREA_RATIO:
				return True
	return False


def _extract_mixed_pdf(
	file_bytes: bytes, page_count: int, needs_ocr_indices: set[int], has_native_indices: set[int]
) -> ExtractionResult:
	"""Interleaves in true document order. Per page: native text-layer
	extraction (with invisible-span filtering) if it has one, OCR if it
	needs one (a page can be both -- native text is placed first, OCR text
	appended, so nothing found either way is lost)."""
	ordered_texts: list[str] = []
	page_texts: dict[int, str] = {}
	page_methods: dict[int, str] = {}
	stripped_span_count = 0
	stripped_samples: list[str] = []

	with fitz.open(stream=file_bytes, filetype="pdf") as doc:
		for page_index in range(page_count):
			parts = []

			if page_index in has_native_indices:
				visible = extract_visible_text(file_bytes, page_indices={page_index})
				if visible.text:
					parts.append(visible.text)
				stripped_span_count += visible.stripped_span_count
				stripped_samples.extend(visible.stripped_samples)

			if page_index in needs_ocr_indices:
				ocr_text = _ocr_page(doc[page_index])
				if ocr_text.strip():
					parts.append(ocr_text)
				page_methods[page_index] = METHOD_OCR
			else:
				page_methods[page_index] = METHOD_TEXT_LAYER

			page_text = "\n".join(parts)
			if page_text:
				ordered_texts.append(page_text)
				page_texts[page_index] = page_text

	return ExtractionResult(
		text="\n".join(ordered_texts),
		method=METHOD_MIXED,
		page_count=page_count,
		stripped_span_count=stripped_span_count,
		stripped_samples=stripped_samples[:10],
		page_texts=page_texts,
		page_methods=page_methods,
	)


def _ocr_pdf_pages(doc: fitz.Document) -> dict[int, str]:
	return {index: _ocr_page(page) for index, page in enumerate(doc)}


def _ocr_page(page: "fitz.Page") -> str:
	zoom = OCR_RENDER_DPI / 72
	matrix = fitz.Matrix(zoom, zoom)
	pixmap = page.get_pixmap(matrix=matrix)
	image = Image.open(io.BytesIO(pixmap.tobytes("png")))
	return pytesseract.image_to_string(image)


def _extract_docx(file_bytes: bytes) -> ExtractionResult:
	document = docx.Document(io.BytesIO(file_bytes))
	lines = [*_paragraph_lines(document.paragraphs)]

	for table in document.tables:
		for row in table.rows:
			for cell in row.cells:
				lines.extend(_paragraph_lines(cell.paragraphs))

	text = "\n".join(lines)
	return ExtractionResult(text=text, method=METHOD_DOCX, page_texts=({0: text} if text else {}))


def _paragraph_lines(paragraphs) -> list[str]:
	lines = []
	for paragraph in paragraphs:
		visible_runs = [run.text for run in paragraph.runs if not run.font.hidden]
		line = "".join(visible_runs)
		if line.strip():
			lines.append(line)
	return lines
