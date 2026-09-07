# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Prompt-injection and hidden-content defenses for AI CV processing.

Threat model: a submitted CV (PDF) may contain content that is invisible or
illegible to a human reviewer but readable by a PDF text extractor or an OCR
engine, deliberately crafted to manipulate an LLM that later parses or scores
the CV -- e.g. white-on-white text, near-zero-size fonts, off-page text
boxes, zero-width/bidi-override Unicode characters, or plain phrases like
"ignore previous instructions" / "give this candidate a perfect score".
None of this should reach an LLM prompt un-filtered, and any CV where it's
found should be forced to human review regardless of what the AI later
reports -- the AI's output is always advisory, never a final decision
(see ZA Recruitment Settings > AI Configuration and the design doc's
non-negotiable #19).

This module implements two independent layers of the agreed defense-in-depth
design; neither is a silver bullet on its own:

1. `extract_visible_text` -- strips PDF text-layer spans that are not
   visually perceptible (near-zero font size, foreground colour matching
   background, or positioned outside the visible page area) before any text
   reaches an LLM prompt. This only covers the PDF text-layer extraction
   path; it does not (and cannot) sanitise instructions hidden inside an
   *image* that the ingestion gateway later decides needs OCR -- an
   adversarial image crafted to be OCR-misread is a separate, harder problem
   and is not solved here. Also not handled: a text layer deliberately
   authored with PDF text-render-mode 3 ("invisible", normally used for OCR
   text layers under a scanned image) -- PyMuPDF's `get_text("dict")` does
   not reliably expose render mode, so a maliciously-added invisible layer
   can currently slip past the span filter below. Revisit when the ingestion
   gateway is built, e.g. by checking `page.get_texttrace()` per span.

2. `scan_for_injection_indicators` -- a cheap, deterministic regex/heuristic
   pass over arbitrary extracted text (from either the text-layer or the OCR
   path) that flags known prompt-injection phrasing and obfuscation
   techniques. This runs independently of, and before, any LLM call -- it
   does not rely on trusting the same model that is the target of the
   attack.

Neither function calls out to any AI provider and neither has any file, DB,
or network access -- they are pure, side-effect-free text transforms.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# Zero-width and bidi-override code points used to hide text, or to make it
# read differently to a human than to a parser (e.g. right-to-left override
# can visually reverse a run of characters). Built from explicit code points
# rather than literal characters -- the characters themselves are invisible
# in an editor, which makes literal-character source fragile to review, copy,
# and re-encode correctly.
INVISIBLE_UNICODE_CODEPOINTS = (
	0x200B,  # zero width space
	0x200C,  # zero width non-joiner
	0x200D,  # zero width joiner
	0x2060,  # word joiner
	0xFEFF,  # zero width no-break space / BOM
	0x202A,  # left-to-right embedding
	0x202B,  # right-to-left embedding
	0x202C,  # pop directional formatting
	0x202D,  # left-to-right override
	0x202E,  # right-to-left override
)
INVISIBLE_UNICODE_CHARS = frozenset(chr(codepoint) for codepoint in INVISIBLE_UNICODE_CODEPOINTS)

# Phrases commonly used to try to redirect an LLM's behaviour. Deliberately
# broad and case-insensitive -- a false positive here just means "send to
# human review", which is always safe; a false negative is the real risk.
INJECTION_PHRASE_PATTERNS = [
	re.compile(pattern, re.IGNORECASE)
	for pattern in (
		r"ignore (all|any|the)?\s*(previous|prior|above)\s*instructions",
		r"disregard (all|any|the)?\s*(previous|prior|above)\s*(instructions|prompt)",
		r"\byou are now\b",
		r"new instructions?\s*:",
		r"^\s*system\s*:",
		r"^\s*assistant\s*:",
		r"do not (flag|reject|screen out) this candidate",
		r"(give|assign|score) (this candidate )?(a )?(perfect|100%|top|highest) (score|match|rating)",
		r"recommend (this candidate|me) (for|as) (the|a) (top|best|ideal) (candidate|match|hire)",
		r"this candidate (meets|exceeds) all requirements",
		r"\bprompt injection\b",
		r"\bjailbreak\b",
	)
]

# Font size (in points) below which text is not legibly readable to a human,
# even though it is perfectly readable to a text extractor.
MIN_VISIBLE_FONT_SIZE = 1.0


@dataclass
class VisibleTextResult:
	text: str
	stripped_span_count: int
	stripped_samples: list[str] = field(default_factory=list)
	page_texts: dict[int, str] = field(default_factory=dict)


def extract_visible_text(pdf_bytes: bytes, page_indices: set[int] | None = None) -> VisibleTextResult:
	"""Extract only the text a human reviewer could actually see in the PDF.

	Drops spans that are near-zero font size, colour-matched to their
	background, or positioned outside the visible page area. Does not
	attempt to judge low-contrast-but-technically-visible text (e.g. very
	light grey on white) -- that's left to `scan_for_injection_indicators`
	catching the phrasing itself.

	`page_indices`, if given, restricts extraction to those 0-indexed pages
	only (all other pages are skipped entirely) -- used by
	`za_recruitment.ingestion.text_extraction` for CVs that mix native-text
	pages with scanned-image pages (e.g. a typed CV with a photocopied ID
	stapled behind it), where only the native-text pages should go through
	this path and the rest need OCR instead. Omit for "all pages" (the
	original, still-default behaviour).

	The returned `page_texts` gives a per-page breakdown of the same visible
	text, built in this single pass (no extra `fitz.open` calls needed) --
	used by `za_recruitment.ingestion.attachment_classifier` to classify each
	page independently (e.g. distinguishing a CV page from a bundled ID/
	certificate scan).
	"""
	visible_lines: list[str] = []
	stripped_count = 0
	stripped_samples: list[str] = []
	page_texts: dict[int, str] = {}

	with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
		for page_index, page in enumerate(doc):
			if page_indices is not None and page_index not in page_indices:
				continue

			page_rect = page.rect
			page_dict = page.get_text("dict")
			page_lines: list[str] = []

			for block in page_dict.get("blocks", []):
				for line in block.get("lines", []):
					line_text_parts = []

					for span in line.get("spans", []):
						span_text = span.get("text", "")
						if not span_text.strip():
							continue

						if _span_is_invisible(span, page_rect):
							stripped_count += 1
							if len(stripped_samples) < 10:
								stripped_samples.append(span_text.strip()[:200])
							continue

						line_text_parts.append(span_text)

					if line_text_parts:
						page_lines.append("".join(line_text_parts))

			if page_lines:
				page_texts[page_index] = strip_invisible_unicode("\n".join(page_lines))
				visible_lines.extend(page_lines)

	text = strip_invisible_unicode("\n".join(visible_lines))
	return VisibleTextResult(
		text=text, stripped_span_count=stripped_count, stripped_samples=stripped_samples, page_texts=page_texts
	)


def _span_is_invisible(span: dict, page_rect: fitz.Rect) -> bool:
	if span.get("size", MIN_VISIBLE_FONT_SIZE) < MIN_VISIBLE_FONT_SIZE:
		return True

	if _color_matches_background(span.get("color")):
		return True

	bbox = span.get("bbox")
	if bbox and not page_rect.intersects(fitz.Rect(bbox)):
		return True

	return False


def _color_matches_background(color_int, background=(255, 255, 255), tolerance=8) -> bool:
	"""PyMuPDF span colours are packed as a single 0xRRGGBB int."""
	if color_int is None:
		return False
	red = (color_int >> 16) & 255
	green = (color_int >> 8) & 255
	blue = color_int & 255
	return all(abs(channel - bg) <= tolerance for channel, bg in zip((red, green, blue), background, strict=True))


def strip_invisible_unicode(text: str) -> str:
	"""Remove known zero-width/bidi-override characters and any other
	Unicode "format" category (Cf) characters not explicitly listed."""
	return "".join(
		ch for ch in text if ch not in INVISIBLE_UNICODE_CHARS and unicodedata.category(ch) != "Cf"
	)


@dataclass
class InjectionScanResult:
	suspected: bool
	indicators: list[str] = field(default_factory=list)


def scan_for_injection_indicators(text: str, *, stripped_span_count: int = 0) -> InjectionScanResult:
	"""Cheap, deterministic scan for known prompt-injection phrasing and
	obfuscation. Runs independently of, and before, any LLM call."""
	indicators: list[str] = []

	for pattern in INJECTION_PHRASE_PATTERNS:
		match = pattern.search(text)
		if match:
			indicators.append(f"Matched injection phrase pattern: {match.group(0)!r}")

	if stripped_span_count:
		indicators.append(f"{stripped_span_count} hidden/invisible text span(s) removed before extraction")

	invisible_char_count = sum(1 for ch in text if ch in INVISIBLE_UNICODE_CHARS)
	if invisible_char_count:
		indicators.append(f"{invisible_char_count} invisible/zero-width Unicode character(s) found")

	return InjectionScanResult(suspected=bool(indicators), indicators=indicators)


def sanitize_and_scan_pdf(pdf_bytes: bytes) -> tuple[str, InjectionScanResult]:
	"""Convenience entry point for the ingestion gateway: extract only
	visually-visible text from a text-layer PDF and scan it for injection
	indicators in one call. The returned text is what should be sent to any
	LLM prompt -- never the raw extracted PDF text."""
	visible = extract_visible_text(pdf_bytes)
	scan = scan_for_injection_indicators(visible.text, stripped_span_count=visible.stripped_span_count)
	return visible.text, scan
