# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Exact-signal candidate matching (design doc §8.1).

This is a deliberately simplified first pass: only exact matches on strong
identity signals (SA ID hash, normalized email, normalized mobile). No
fuzzy/weighted scoring across name/DOB/address (§8.4) yet -- that needs the
`*_match_weight` fields already sitting on `ZA Recruitment Settings` waiting
for it, but implementing the scoring algorithm itself is a later phase.
Name similarity alone must never automatically merge candidates (§8.4) --
this module doesn't even look at name, by design.

Not concurrency-safe (§8.5): two simultaneous submissions with the same new
identity signals can both fail to find an existing match and both create a
new `ZA Candidate`. Acceptable for a single-operator manual-upload flow;
must be revisited (locking / DB-level constraints) before any concurrent
(public web form, bulk, email/SharePoint polling) intake path goes live.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe

from za_recruitment.candidate.identity import hash_value, normalize_sa_id_number
from za_recruitment.candidate.normalization import normalize_email, normalize_mobile

# Mirrors the "Confirmed Existing Candidate" / "Probable Existing Candidate"
# vocabulary already defined on ZA Recruitment Ingestion.match_result.
MATCH_NEW = "New Candidate"
MATCH_CONFIRMED = "Confirmed Existing Candidate"
MATCH_PROBABLE = "Probable Existing Candidate"


@dataclass
class MatchResult:
	candidate_name: str | None
	match_result: str
	matched_on: str | None  # "id_number" | "email" | "mobile" | None


def find_matching_candidate(
	*,
	email: str | None = None,
	mobile: str | None = None,
	sa_id_number: str | None = None,
) -> MatchResult:
	"""Strongest signal first: a valid SA ID number is treated as a
	confirmed match (it's unique per person and hard to type wrong by
	accident since it must pass a checksum); an exact normalized-email match
	is also confirmed; an exact normalized-mobile-only match is treated as
	only probable, since phone numbers get reassigned/shared, and the
	resulting candidate is flagged `duplicate_review_required` by the caller.
	"""
	id_hash = hash_value(normalize_sa_id_number(sa_id_number))
	if id_hash:
		name = frappe.db.get_value("ZA Candidate", {"id_number_hash": id_hash}, "name")
		if name:
			return MatchResult(candidate_name=name, match_result=MATCH_CONFIRMED, matched_on="id_number")

	normalized_email = normalize_email(email)
	if normalized_email:
		name = frappe.db.get_value("ZA Candidate", {"normalized_email": normalized_email}, "name")
		if name:
			return MatchResult(candidate_name=name, match_result=MATCH_CONFIRMED, matched_on="email")

	normalized_mobile = normalize_mobile(mobile)
	if normalized_mobile:
		name = frappe.db.get_value("ZA Candidate", {"normalized_mobile": normalized_mobile}, "name")
		if name:
			return MatchResult(candidate_name=name, match_result=MATCH_PROBABLE, matched_on="mobile")

	return MatchResult(candidate_name=None, match_result=MATCH_NEW, matched_on=None)
