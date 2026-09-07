# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Resolve an incoming submission to a canonical `ZA Candidate` -- creating
one if no exact-signal match is found, per the "one person, one candidate
record" principle (design doc §2.2/§8)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import frappe
from frappe.utils import today

from za_recruitment.candidate.candidate_matcher import (
	MATCH_NEW,
	MATCH_PROBABLE,
	MatchResult,
	find_matching_candidate,
)
from za_recruitment.candidate.identity import (
	extract_dob_from_sa_id,
	extract_gender_from_sa_id,
	hash_value,
	normalize_sa_id_number,
)
from za_recruitment.candidate.normalization import normalize_email, normalize_mobile


@dataclass
class CandidateResolution:
	candidate: "frappe.model.document.Document"
	match_result: str
	is_new: bool


def resolve_or_create_candidate(
	*,
	first_names: str | None,
	surname: str | None,
	email: str | None = None,
	mobile: str | None = None,
	sa_id_number: str | None = None,
	date_of_birth: date | None = None,
	gender: str | None = None,
	languages: str | None = None,
	physical_address: str | None = None,
	highest_education_level: str | None = None,
	settings=None,
) -> CandidateResolution:
	settings = settings or frappe.get_single("ZA Recruitment Settings")

	if settings.enable_candidate_deduplication:
		match = find_matching_candidate(email=email, mobile=mobile, sa_id_number=sa_id_number)
	else:
		match = _no_match()

	sa_id_number = normalize_sa_id_number(sa_id_number)
	# Free and deterministic once we have a valid ID number, regardless of
	# where it came from -- never overrides an explicitly extracted/typed
	# value, only fills a gap.
	date_of_birth = date_of_birth or extract_dob_from_sa_id(sa_id_number)
	gender = gender or extract_gender_from_sa_id(sa_id_number)

	if match.candidate_name:
		candidate = frappe.get_doc("ZA Candidate", match.candidate_name)
		candidate.last_submission = today()
		if match.match_result == MATCH_PROBABLE:
			candidate.duplicate_review_required = 1
		_fill_profile_field_gaps(
			candidate,
			sa_id_number=sa_id_number,
			date_of_birth=date_of_birth,
			gender=gender,
			languages=languages,
			physical_address=physical_address,
			highest_education_level=highest_education_level,
		)
		candidate.save(ignore_permissions=True)
		return CandidateResolution(candidate=candidate, match_result=match.match_result, is_new=False)

	candidate = frappe.get_doc(
		{
			"doctype": "ZA Candidate",
			"first_names": first_names,
			"surname": surname,
			"primary_email": email,
			"normalized_email": normalize_email(email),
			"mobile_number": mobile,
			"normalized_mobile": normalize_mobile(mobile),
			"sa_id_number": sa_id_number,
			"id_number_hash": hash_value(sa_id_number),
			"date_of_birth": date_of_birth,
			"gender": gender,
			"languages": languages,
			"physical_address": physical_address,
			"highest_education_level": highest_education_level,
			"candidate_status": "Active",
			"first_received": today(),
			"last_submission": today(),
		}
	)
	candidate.insert(ignore_permissions=True)
	return CandidateResolution(candidate=candidate, match_result=MATCH_NEW, is_new=True)


def _fill_profile_field_gaps(candidate, **fields) -> None:
	"""Fills whatever the existing candidate record doesn't already have --
	never overwrites a value that's already on file."""
	for fieldname, value in fields.items():
		if value and not candidate.get(fieldname):
			candidate.set(fieldname, value)


def _no_match() -> MatchResult:
	return MatchResult(candidate_name=None, match_result=MATCH_NEW, matched_on=None)
