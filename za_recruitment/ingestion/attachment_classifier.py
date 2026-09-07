# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Classifies the non-CV pages of a submitted CV into supporting-document
attachments (ID copy, qualification certificates, driver's licence, proof
of residence, etc.) -- confirmed against two real CV submissions to be an
extremely common shape: a typed CV page followed by scanned photocopies of
supporting documents stapled behind it.

Deliberately page-level, not sub-page: a single scanned page that is itself
a collage of multiple physical documents (a real example seen: one page
with an ID book photo, an operator's licence card, and a police verification
stamp all glued onto it) is classified as ONE attachment record using
whichever `ZA Attachment Type`'s keywords score highest against that page's
combined text -- there is no sub-page segmentation/cropping here. Heuristic
keyword matching, not AI-grade classification (that's Phase 5's
`ZA AI Candidate Profile`) -- a false negative just means a new
`ZA Attachment Type` gets auto-created (or the page falls back to "Other"),
never a false positive against existing candidate data.

Every attachment produced here has `location = "Attached to CV"` -- this
module never crops/extracts a page into its own standalone File; that's a
distinct, harder feature ("Attached Here" stays available for a human to
use manually, or for a future channel that already receives documents as
separate files).

Keyword scoring is weighted by phrase length (word count), not a flat +1 per
match -- confirmed against a real submission where a National Senior
Certificate page legitimately contains both "identity number" and
"Republic of South Africa" (SA certificates print the ID number and a
coat-of-arms header too), and those generic, short phrases tied with the
NSC-specific ones under flat scoring, misclassifying the page as an ID
document purely because of dict ordering. Weighting means longer, more
specific phrases ("national senior certificate") dominate short generic
ones ("identity number") instead of counting equally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import frappe

# Below this many characters, a page has nothing worth classifying (e.g. a
# near-blank separator page).
MIN_PAGE_TEXT_CHARS = 15

# Matching this type on a page means "this is just more of the CV", not a
# separate attachment -- see _is_cv_continuation.
CV_TYPE_NAME = "CV / Resume"

# Pre-seeded fallback (za_recruitment/fixtures/za_attachment_type.json) used
# when an OCR'd page matches nothing and no document title could be guessed.
FALLBACK_TYPE_NAME = "Other"

# Heading-like lines that are document titles, not names: reasonably long
# (short 1-2 word fragments are far more likely to be a mid-sentence OCR
# fragment than a real title -- confirmed against a real submission where
# "Sin herdie", a fragment of garbled Afrikaans legal boilerplate, was
# grammatically plausible-looking enough to slip past a shorter minimum),
# mostly capitalized, no more than a handful of words.
_TITLE_LINE_RE = re.compile(r"^[A-Z][A-Za-z0-9&/.,'\-\s]{11,60}$")
_TITLE_DENYLIST = {"personal information", "particulars of holder"}
_TITLE_SCAN_LINES = 20

_VOWELS = frozenset("aeiouAEIOU")


@dataclass
class ClassifiedAttachment:
	attachment_type: str
	description: str
	page_range: str
	confidence: float


def classify_pages(page_texts: dict[int, str], page_methods: dict[int, str] | None = None) -> list[ClassifiedAttachment]:
	"""`page_texts` is 0-indexed page -> extracted text (from
	`ingestion.text_extraction.ExtractionResult.page_texts`). `page_methods`,
	if given, maps the same indices to how that page was extracted ("Text
	Layer" vs "OCR") -- used to decide whether an unmatched page is just more
	native CV text (skip) or a scanned document of an unrecognised kind
	(classify as "Other" / auto-create a type). Wrap calls to this function in
	a try/except at the call site -- a classification bug must never block
	the CV/candidate acceptance that already happened before this runs.
	"""
	page_methods = page_methods or {}
	active_types = _get_active_types()
	results: list[ClassifiedAttachment] = []

	for page_index in sorted(page_texts):
		text = page_texts[page_index]
		if len(text.strip()) < MIN_PAGE_TEXT_CHARS:
			continue

		best_type, score = _best_match(text, active_types)
		method = page_methods.get(page_index, "Text Layer")
		is_scanned = method == "OCR"

		if best_type is None or best_type["attachment_type_name"] == CV_TYPE_NAME:
			if not is_scanned:
				continue  # native CV text with no other strong match -- just more of the CV
			best_type, description = _resolve_unmatched_scanned_page(text)
			score = 0
		else:
			description = _guess_title(text) or best_type["attachment_type_name"]

		results.append(
			ClassifiedAttachment(
				attachment_type=best_type["attachment_type_name"],
				description=description,
				page_range=str(page_index + 1),
				confidence=_confidence(score),
			)
		)

	return results


def _get_active_types() -> list[dict]:
	return frappe.get_all(
		"ZA Attachment Type",
		filters={"active": 1},
		fields=["name", "attachment_type_name", "keywords"],
	)


def _best_match(text: str, types: list[dict]) -> tuple[dict | None, int]:
	lowered = text.lower()
	best_type = None
	best_score = 0

	for attachment_type in types:
		keywords = [k.strip().lower() for k in (attachment_type.get("keywords") or "").split(",") if k.strip()]
		score = sum(len(keyword.split()) for keyword in keywords if keyword in lowered)
		if score > best_score:
			best_type, best_score = attachment_type, score

	return best_type, best_score


def _resolve_unmatched_scanned_page(text: str) -> tuple[dict, str]:
	"""An OCR'd page that matched no known type -- either grow the taxonomy
	from a guessed document title, or fall back to the pre-seeded "Other"
	type (with a raw text snippet as the description, so a human reviewer
	still has something to go on) so the page is never silently dropped."""
	title = _guess_title(text)
	if title:
		type_doc = _get_or_create_type(title)
		return type_doc, title

	type_doc = _get_or_create_type(FALLBACK_TYPE_NAME, system_generated=False)
	return type_doc, f"Unclassified scanned page -- raw text: {_snippet(text)}"


def _get_or_create_type(attachment_type_name: str, *, system_generated: bool = True) -> dict:
	existing = frappe.db.get_value(
		"ZA Attachment Type", attachment_type_name, ["name", "attachment_type_name"], as_dict=True
	)
	if existing:
		return existing

	frappe.get_doc(
		{
			"doctype": "ZA Attachment Type",
			"attachment_type_name": attachment_type_name,
			"category": "Other",
			"active": 1,
			"system_generated": 1 if system_generated else 0,
			"description": "Auto-created by za_recruitment.ingestion.attachment_classifier from a scanned page whose document title didn't match any known type."
			if system_generated
			else None,
		}
	).insert(ignore_permissions=True)

	return {"name": attachment_type_name, "attachment_type_name": attachment_type_name}


def _guess_title(text: str) -> str | None:
	"""Best-effort document-title guess from the page's own heading-like
	text, e.g. "NATIONAL SENIOR CERTIFICATE" or "CERTIFICATE OF COMPETENCE".
	Scans the first ~20 lines (a useful title can appear after some OCR
	noise, not necessarily first), skipping anything that doesn't pass
	_looks_like_real_text -- badly garbled OCR must never become a
	fabricated taxonomy entry."""
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	for line in lines[:_TITLE_SCAN_LINES]:
		if line.lower() in _TITLE_DENYLIST:
			continue
		if not _TITLE_LINE_RE.fullmatch(line):
			continue
		if len(line.split()) < 2:
			continue
		if not _looks_like_real_text(line):
			continue
		return line.title() if line.isupper() else line
	return None


def _looks_like_real_text(line: str) -> bool:
	"""Cheap sanity gate against badly garbled OCR being mistaken for a real
	document title -- confirmed against a real submission where a driving
	licence's near-total-gibberish OCR output ("BuenjuQ OWZSWH W penresey")
	would otherwise have been accepted as a plausible-looking title and
	turned into a fabricated ZA Attachment Type."""
	for word in line.split():
		letters = [ch for ch in word if ch.isalpha()]
		if not letters:
			continue

		joined = "".join(letters)
		# Real titles are consistently ALL-CAPS ("NATIONAL"), Title-Case
		# ("License"), or all-lowercase -- OCR corruption instead produces a
		# stray capital buried mid-word in no consistent pattern (e.g.
		# "BuenjuQ", "OWZSWH"); a word matching none of the normal casing
		# shapes is the actual corruption signal, not uppercase count alone
		# (which would also -- wrongly -- flag every legitimate ALL-CAPS
		# heading).
		if not (joined.isupper() or joined.islower() or joined.istitle()):
			return False

		if len(letters) >= 4:
			vowel_ratio = sum(1 for ch in letters if ch in _VOWELS) / len(letters)
			if not (0.2 <= vowel_ratio <= 0.7):
				return False
	return True


def _snippet(text: str, length: int = 80) -> str:
	flat = " ".join(text.split())
	return flat[:length] + ("..." if len(flat) > length else "")


def _confidence(score: int) -> float:
	"""Heuristic, not statistically calibrated -- more/longer matched
	keyword phrases means more confident, capped at 1.0."""
	return min(1.0, score / 5.0)
