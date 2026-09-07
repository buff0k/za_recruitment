# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Normalization helpers for candidate identity signals.

Used both when writing a `ZA Candidate` (to populate `normalized_email` /
`normalized_mobile`) and when matching an incoming submission against
existing candidates (`za_recruitment.candidate.candidate_matcher`) -- the
same normalization must be applied on both sides or matches will silently
fail.

Both functions return `None` for missing/unparseable input rather than an
empty string -- callers should not write `""` into the normalized fields
(an empty string is a real, colliding value for exact-match lookups, unlike
`None`/absent).
"""

from __future__ import annotations

import re

# South African mobile numbers: 10 digits starting 0, or +27 (or 27) followed
# by 9 digits. We normalize everything to E.164-ish "+27XXXXXXXXX".
_SA_MOBILE_DIGITS_RE = re.compile(r"\D+")


def normalize_email(email: str | None) -> str | None:
	if not email:
		return None
	email = email.strip().lower()
	return email or None


def normalize_mobile(mobile: str | None) -> str | None:
	if not mobile:
		return None

	digits = _SA_MOBILE_DIGITS_RE.sub("", mobile)
	if not digits:
		return None

	# Strip a leading international/trunk prefix down to the 9-digit
	# subscriber number, then re-apply the +27 country code consistently.
	if digits.startswith("0027"):
		digits = digits[4:]
	elif digits.startswith("27") and len(digits) == 11:
		digits = digits[2:]
	elif digits.startswith("0") and len(digits) == 10:
		digits = digits[1:]

	if len(digits) != 9 or not digits.isdigit():
		# Not a recognisable SA mobile number -- return the digit-only form
		# rather than guessing, so it still normalizes consistently for
		# exact-match comparison even if it's e.g. a foreign number.
		return digits

	return f"+27{digits}"
