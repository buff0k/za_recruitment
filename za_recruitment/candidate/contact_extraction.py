# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Deterministic (non-AI) fallback extraction of candidate profile fields --
name, email, mobile, SA ID number, date of birth, gender, languages, and a
raw address block -- from a CV's extracted text.

This exists because the ingestion gateway needs *something* to identify and
profile a candidate by, and requiring a human to always type these details
into the Desk upload dialog defeats the point of unsolicited/bulk CV intake
(design doc §2.1/§3) -- the overwhelming majority of real CVs state these
details plainly in labeled fields. This is only ever used to fill in
whatever the caller didn't already provide; it never overrides an
explicitly supplied value.

Deliberately heuristic and best-effort, not AI-grade -- full free-text
parsing is not a solved problem without a real NLP/AI step
(`ai.llm_profile_extraction`, tried next if this still leaves gaps). A false
negative here just means the next tier has to work harder; there is no
false-positive risk to existing data since this only ever fills gaps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from za_recruitment.candidate.identity import is_valid_sa_id_number, normalize_sa_id_number

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Loose South African phone-number pattern: 0XX XXX XXXX or +27/0027 XX XXX
# XXXX, with optional spaces/dashes as separators. normalize_mobile()
# downstream re-validates/normalizes properly -- this just needs to find
# candidates. Matched separately from *which* candidate is actually the
# person's mobile -- see _best_mobile_match: a CV bundled with other scanned
# documents (a municipal bill, say) can easily contain more than one
# phone-shaped number, and a landline isn't the candidate's mobile.
_PHONE_RE = re.compile(r"(?:\+27|0027|0)[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{4}\b")
_SA_MOBILE_PREFIXES = ("06", "07", "08")

# Labeled-field patterns common on South African CV templates, e.g.
# "Surname : Thabede" / "First Names: Ntombizodwa" / "Name: Henry Kriek".
_SURNAME_LABEL_RE = re.compile(r"^\s*surname\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_FIRST_NAMES_LABEL_RE = re.compile(
	r"^\s*first\s*name[s]?\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE
)
_FULL_NAME_LABEL_RE = re.compile(r"^\s*name\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# "CURRICULUM VITAE OF <NAME>" / "CV OF <NAME>" -- another very common
# South African CV convention.
_CV_OF_NAME_RE = re.compile(
	r"CURRICULUM\s+VITAE\s+OF\s+([A-Z][A-Z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3})", re.IGNORECASE
)

_ID_NUMBER_LABEL_RE = re.compile(
	r"^\s*(?:sa\s*)?(?:identity\s*number|id\s*no\.?|id\s*number)\s*[:\-]\s*([\d\s]{10,17})$",
	re.IGNORECASE | re.MULTILINE,
)
# Fallback for an unlabeled 13-digit run (spaces allowed, as SA IDs are often
# printed) -- only accepted if it passes the checksum, so this can't
# mistake an arbitrary 13-digit number (e.g. an account number) for an ID.
_BARE_ID_NUMBER_RE = re.compile(r"\b\d{6}[\s\-]?\d{4}[\s\-]?\d{2,3}\b")

_DOB_LABEL_RE = re.compile(r"^\s*date\s*of\s*birth\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_DOB_FORMATS = ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")

_GENDER_LABEL_RE = re.compile(r"^\s*(?:gender|sex)\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)

_LANGUAGES_LABEL_RE = re.compile(
	r"^\s*(?:spoken\s*language[s]?|languages?\s*spoken|languages?)\s*[:\-]\s*(.+)$",
	re.IGNORECASE | re.MULTILINE,
)

_ADDRESS_LABEL_RE = re.compile(
	r"^\s*(?:physical|residential|home|postal)?\s*address\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE
)
_BARE_POSTAL_CODE_LINE_RE = re.compile(r"^\d{4}$")
_CV_HEADING_START_RE = re.compile(r"^(curriculum\s+vitae|cv\b|resume)", re.IGNORECASE)

# Lines that look like a personal name (2-4 capitalized words, letters/
# apostrophes/hyphens only) but are actually a CV section heading, so should
# never be mistaken for the candidate's name by the near-top-of-document
# fallback heuristic.
_HEADING_DENYLIST = {
	"curriculum vitae",
	"personal information",
	"personal details",
	"contact details",
	"contact information",
	"work experience",
	"academic qualification",
	"other qualification",
	"educational background",
	"employment history",
	"professional profile",
	"career objective",
	"reference available",
}


# Keyword -> exact `ZA Education Level.education_level` name, matched
# in order (first match wins) -- covers the common cases for free/instant,
# no AI needed. Names match the seed fixture exactly
# (za_recruitment/fixtures/za_education_level.json); anything not covered
# here just falls through to the LLM tier, which is given the live list of
# active levels to choose from instead of a hardcoded copy.
_EDUCATION_LEVEL_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
	(("doctorate", "phd", "ph.d"), "Doctorate"),
	(("master's", "masters", "m.sc", "msc"), "Master's Degree"),
	(("postgraduate diploma",), "Postgraduate Diploma"),
	(("honours", "honors"), "Honours"),
	(("bachelor", "b.sc", "b.com", "b.a "), "Bachelor's Degree"),
	(("advanced diploma",), "Advanced Diploma"),
	(("diploma",), "Diploma"),
	(("higher certificate",), "Higher Certificate"),
	(("trade test", "trade tested", "trade qualification"), "Trade Qualification"),
	(("occupational certificate",), "Occupational Certificate"),
	(("national senior certificate", "matric", "grade 12", "nsc"), "National Senior Certificate / Grade 12"),
	(("grade 11",), "Grade 11"),
	(("grade 10",), "Grade 10"),
	(("grade 9",), "Grade 9"),
	(("primary school",), "Primary School"),
	(("no formal education",), "No Formal Education"),
)


@dataclass
class IdentityHints:
	first_names: str | None = None
	surname: str | None = None
	email: str | None = None
	mobile: str | None = None
	sa_id_number: str | None = None
	date_of_birth: date | None = None
	gender: str | None = None
	languages: str | None = None
	physical_address: str | None = None
	highest_education_level: str | None = None


def extract_identity_hints(text: str) -> IdentityHints:
	if not text:
		return IdentityHints()

	return IdentityHints(
		email=_first_match(_EMAIL_RE, text),
		mobile=_best_mobile_match(text),
		sa_id_number=_extract_id_number(text),
		date_of_birth=_extract_date_of_birth(text),
		gender=_extract_gender(text),
		languages=_labeled_value(_LANGUAGES_LABEL_RE, text),
		physical_address=_extract_address(text),
		highest_education_level=_extract_education_level(text),
		**_extract_name(text),
	)


def _first_match(pattern: re.Pattern, text: str) -> str | None:
	match = pattern.search(text)
	return match.group(0).strip() if match else None


def _best_mobile_match(text: str) -> str | None:
	"""A CV bundled with other scanned documents (proof of residence, an ID
	book) can contain more than one phone-shaped number -- confirmed against
	a real submission where the only number found was a municipality's
	landline, wrongly stored as the candidate's mobile. Prefers a number
	whose prefix is a real SA mobile range (06/07/08) over anything else,
	rather than just taking the first match in document order."""
	matches = [m.group(0) for m in _PHONE_RE.finditer(text)]
	if not matches:
		return None

	mobile_shaped = [m for m in matches if _looks_like_mobile_number(m)]
	return (mobile_shaped or matches)[0]


def _looks_like_mobile_number(raw: str) -> bool:
	digits = re.sub(r"\D", "", raw)
	if digits.startswith("0027"):
		digits = digits[4:]
	elif digits.startswith("27") and len(digits) == 11:
		digits = "0" + digits[2:]
	return digits.startswith(_SA_MOBILE_PREFIXES)


def _extract_id_number(text: str) -> str | None:
	labeled = _labeled_value(_ID_NUMBER_LABEL_RE, text)
	if labeled and is_valid_sa_id_number(labeled):
		return normalize_sa_id_number(labeled)

	for match in _BARE_ID_NUMBER_RE.finditer(text):
		candidate = normalize_sa_id_number(match.group(0))
		if candidate and is_valid_sa_id_number(candidate):
			return candidate

	return None


def _extract_date_of_birth(text: str) -> date | None:
	labeled = _labeled_value(_DOB_LABEL_RE, text)
	return parse_date_string(labeled) if labeled else None


def parse_date_string(raw: str | None) -> date | None:
	"""Shared with ai.llm_profile_extraction -- an LLM-returned date is just
	another raw string that needs the same tolerant parsing as a
	regex-captured one, not a reason to duplicate the format list."""
	if not raw:
		return None
	cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE).strip()
	for fmt in _DOB_FORMATS:
		try:
			return datetime.strptime(cleaned, fmt).date()
		except ValueError:
			continue
	return None


def _extract_gender(text: str) -> str | None:
	labeled = _labeled_value(_GENDER_LABEL_RE, text)
	if not labeled:
		return None
	lowered = labeled.lower()
	if "female" in lowered or lowered.strip() == "f":
		return "Female"
	if "male" in lowered or lowered.strip() == "m":
		return "Male"
	return None


def _extract_address(text: str) -> str | None:
	labeled = _labeled_value(_ADDRESS_LABEL_RE, text)
	if labeled:
		return labeled
	return _guess_leading_address_block(text)


def _guess_leading_address_block(text: str) -> str | None:
	"""Some CVs (confirmed against a real submission) print the address as
	the first few unlabeled lines of the document, ending in a bare postal
	code, before the "CURRICULUM VITAE" heading -- not a generic enough
	shape to trust broadly, so only accepted when it ends in exactly that
	recognisable postal-code line."""
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	block: list[str] = []
	for line in lines[:6]:
		if _CV_HEADING_START_RE.match(line):
			break
		block.append(line)
		if _BARE_POSTAL_CODE_LINE_RE.match(line):
			return "\n".join(block)
	return None


def _extract_education_level(text: str) -> str | None:
	lowered = text.lower()
	for keywords, level_name in _EDUCATION_LEVEL_KEYWORDS:
		if any(keyword in lowered for keyword in keywords):
			return level_name
	return None


def _extract_name(text: str) -> dict:
	"""Resolves first_names/surname independently and merges whatever each
	strategy finds, rather than requiring one strategy to fully succeed
	before trying the next. A real CV (confirmed against an actual
	submission) paired a `Surname:` label from one document with a generic
	`Name:` label from another bundled document instead of `First Names:` --
	discarding the found surname just because the *specific* first-names
	label wasn't also present was a real bug, not a hypothetical one."""
	surname = _labeled_value(_SURNAME_LABEL_RE, text)
	first_names = _labeled_value(_FIRST_NAMES_LABEL_RE, text)

	if not (surname and first_names):
		full_name = _labeled_value(_FULL_NAME_LABEL_RE, text)
		if full_name and _looks_like_a_name(full_name):
			if surname and not first_names:
				# A separate surname was already found elsewhere -- a nearby
				# generic "Name:" label is almost always the given name(s)
				# half of that same pair, not a full name to split.
				first_names = full_name
			elif not surname and not first_names:
				# No separate surname found at all -- "Name:" is most likely
				# the full name on its own.
				guess = _split_full_name(full_name)
				surname = guess["surname"]
				first_names = guess["first_names"]

	if surname or first_names:
		return {"surname": _title_case(surname), "first_names": _title_case(first_names)}

	cv_of_match = _CV_OF_NAME_RE.search(text)
	if cv_of_match:
		return _split_full_name(cv_of_match.group(1))

	heading_line = _find_name_like_heading(text)
	if heading_line:
		return _split_full_name(heading_line)

	return {"first_names": None, "surname": None}


def _labeled_value(pattern: re.Pattern, text: str) -> str | None:
	match = pattern.search(text)
	return _clean_name_value(match.group(1)) if match else None


def _title_case(value: str | None) -> str | None:
	# OCR'd labeled fields are frequently all-caps ("Surname: THABEDE") --
	# normalize casing the same way _split_full_name already does for its
	# own guesses, so every path produces consistent Title Case.
	return value.title() if value else None


def _clean_name_value(value: str) -> str:
	# Labeled-field values sometimes trail into the next label on the same
	# line in poorly-formatted CVs (rare) -- keep only the first "column".
	return value.strip().strip(":-").strip()


def _split_full_name(full_name: str) -> dict:
	parts = full_name.split()
	if len(parts) < 2:
		return {"first_names": full_name.title() if full_name else None, "surname": None}
	return {
		"first_names": " ".join(parts[:-1]).title(),
		"surname": parts[-1].title(),
	}


def _looks_like_a_name(value: str) -> bool:
	if not value or value.lower() in _HEADING_DENYLIST:
		return False
	words = value.split()
	if not (1 <= len(words) <= 4):
		return False
	return all(re.fullmatch(r"[A-Za-z'\-]+", word) for word in words)


def _find_name_like_heading(text: str) -> str | None:
	"""Best-effort fallback for CVs with no labeled fields: the candidate's
	name is very commonly the first prominent line of the document (e.g. a
	name printed as a heading above a job title), so scan the first ~15
	non-empty lines for one that looks like a name and isn't a known CV
	section heading."""
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	for line in lines[:15]:
		if _looks_like_a_name(line):
			return line
	return None
