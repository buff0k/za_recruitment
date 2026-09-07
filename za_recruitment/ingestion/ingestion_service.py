# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Lifecycle helpers over `ZA Recruitment Ingestion` -- the audit record for
every attempt to bring a CV into the platform (design doc §5.4), including
attempts that never end up producing a permanent File."""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime


def create_ingestion(
	*,
	source_type: str,
	recruitment_source: str | None,
	original_filename: str | None,
) -> "frappe.model.document.Document":
	ingestion = frappe.get_doc(
		{
			"doctype": "ZA Recruitment Ingestion",
			"source_type": source_type,
			"recruitment_source": recruitment_source,
			"original_filename": original_filename,
			"received_on": now_datetime(),
			"status": "Received",
			"processing_started": now_datetime(),
		}
	)
	ingestion.insert(ignore_permissions=True)
	return ingestion


def set_stage(ingestion, stage: str, status: str | None = None) -> None:
	ingestion.processing_stage = stage
	if status:
		ingestion.status = status
	ingestion.save(ignore_permissions=True)


def record_temp_file_metadata(ingestion, *, file_size: int, mime_type: str | None, sha256: str) -> None:
	ingestion.file_size = file_size
	ingestion.mime_type = mime_type
	ingestion.sha_256 = sha256
	ingestion.save(ignore_permissions=True)


def record_extraction(ingestion, *, method: str) -> None:
	ingestion.extraction_method = method
	ingestion.save(ignore_permissions=True)


def record_prompt_injection_scan(ingestion, *, suspected: bool, indicators: list[str]) -> None:
	ingestion.prompt_injection_suspected = 1 if suspected else 0
	ingestion.prompt_injection_indicators = "\n".join(indicators) if indicators else None
	ingestion.save(ignore_permissions=True)


def reject(ingestion, reason: str) -> None:
	ingestion.status = "Rejected"
	ingestion.rejection_reason = reason
	ingestion.accepted = 0
	ingestion.processing_completed = now_datetime()
	ingestion.save(ignore_permissions=True)


def mark_duplicate(ingestion, *, duplicate_cv_record: str, candidate: str | None) -> None:
	ingestion.status = "Duplicate"
	ingestion.duplicate_cv = 1
	ingestion.duplicate_cv_record = duplicate_cv_record
	ingestion.candidate = candidate
	ingestion.accepted = 0
	ingestion.processing_completed = now_datetime()
	ingestion.save(ignore_permissions=True)


def finalize_accepted(
	ingestion,
	*,
	candidate: str,
	candidate_cv: str,
	match_result: str,
	status: str = "Accepted",
) -> None:
	ingestion.status = status
	ingestion.candidate = candidate
	ingestion.match_result = match_result
	ingestion.accepted = 1
	ingestion.processing_completed = now_datetime()
	ingestion.save(ignore_permissions=True)


def record_error(ingestion, error: str) -> None:
	ingestion.status = "Failed"
	ingestion.error = error
	ingestion.retry_count = (ingestion.retry_count or 0) + 1
	ingestion.processing_completed = now_datetime()
	ingestion.save(ignore_permissions=True)
