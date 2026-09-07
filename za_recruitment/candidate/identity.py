# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""South African identity-document helpers.

`hash_value` is used for both `ZA Candidate.id_number_hash` and
`.passport_hash` -- a one-way digest so exact-match candidate lookups don't
require comparing/indexing the plaintext identity number directly.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

_DIGITS_RE = re.compile(r"\D+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_sa_id_number(id_number: str | None) -> str | None:
	if not id_number:
		return None
	digits = _DIGITS_RE.sub("", id_number)
	return digits or None


def is_valid_sa_id_number(id_number: str | None) -> bool:
	"""13 digits, valid Luhn checksum. Does not verify the date-of-birth
	segment is a real calendar date beyond basic range checks -- that's done
	separately by `extract_dob_from_sa_id`, which returns None rather than
	raising if it can't produce a real date."""
	digits = normalize_sa_id_number(id_number)
	if not digits or len(digits) != 13:
		return False
	return _luhn_checksum_valid(digits)


def _luhn_checksum_valid(digits: str) -> bool:
	total = 0
	for index, char in enumerate(reversed(digits)):
		value = int(char)
		if index % 2 == 1:
			value *= 2
			if value > 9:
				value -= 9
		total += value
	return total % 10 == 0


def extract_dob_from_sa_id(id_number: str | None, *, today: date | None = None) -> date | None:
	"""First 6 digits are YYMMDD. Century is inferred: a YY greater than the
	current two-digit year is assumed to be 1900s, otherwise 2000s -- the
	usual heuristic for this ID format, good enough for this pass. Returns
	None (never raises) if the digits don't form a real calendar date."""
	digits = normalize_sa_id_number(id_number)
	if not digits or len(digits) != 13:
		return None

	today = today or date.today()
	yy, mm, dd = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
	century = 2000 if yy <= today.year % 100 else 1900

	try:
		return date(century + yy, mm, dd)
	except ValueError:
		return None


def extract_gender_from_sa_id(id_number: str | None) -> str | None:
	"""Digits 7-10 (the gender sequence number): 0000-4999 = Female,
	5000-9999 = Male."""
	digits = normalize_sa_id_number(id_number)
	if not digits or len(digits) != 13:
		return None
	sequence = int(digits[6:10])
	return "Female" if sequence < 5000 else "Male"


@dataclass
class SAIdInfo:
	valid: bool
	date_of_birth: date | None
	gender: str | None


def inspect_sa_id_number(id_number: str | None) -> SAIdInfo:
	return SAIdInfo(
		valid=is_valid_sa_id_number(id_number),
		date_of_birth=extract_dob_from_sa_id(id_number),
		gender=extract_gender_from_sa_id(id_number),
	)


def hash_value(value: str | None) -> str | None:
	"""One-way sha256 hex digest, used for `id_number_hash` / `passport_hash`.
	Strips *all* whitespace (not just leading/trailing -- passport numbers
	such as "A 12345" are commonly written with an internal space) and
	uppercases first, so formatting differences don't produce different
	hashes for the same underlying document."""
	if not value:
		return None
	normalized = _WHITESPACE_RE.sub("", value).upper()
	if not normalized:
		return None
	return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
