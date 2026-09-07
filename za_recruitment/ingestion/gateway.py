# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""The ingestion gateway (design doc §7/§30): the single pipeline every CV
intake channel (manual Desk upload today; public web form / email /
SharePoint later) must go through -- validation, hashing, duplicate
detection, text extraction, prompt-injection scanning, candidate
resolution, and CV versioning. Same rules regardless of source (§2.4).

`ingest_cv()` is pure orchestration over bytes -- it does not touch the
Frappe `File` doctype at all, so it's fully unit-testable from a console
without any HTTP/upload machinery, and channel-specific wrappers (like
`upload_cv_manual` below) stay responsible for their own File lifecycle.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from datetime import date

import frappe
from frappe.utils import today

from za_recruitment.ai.prompt_injection import InjectionScanResult, scan_for_injection_indicators
from za_recruitment.candidate.candidate_service import resolve_or_create_candidate
from za_recruitment.candidate.contact_extraction import extract_identity_hints
from za_recruitment.candidate.llm_profile_extraction import extract_profile_via_llm
from za_recruitment.ingestion import ingestion_service
from za_recruitment.ingestion.attachment_classifier import classify_pages
from za_recruitment.ingestion.duplicate_service import find_cv_by_hash, sha256_bytes
from za_recruitment.ingestion.text_extraction import extract_cv_text
from za_recruitment.ingestion.validator import validate_upload

STATUS_ACCEPTED = "Accepted"
STATUS_REVIEW_REQUIRED = "Review Required"
STATUS_DUPLICATE = "Duplicate"
STATUS_REJECTED = "Rejected"
STATUS_FAILED = "Failed"


@dataclass
class IngestionOutcome:
	status: str
	message: str
	ingestion: str
	candidate: str | None = None
	candidate_is_new: bool = False
	candidate_cv: str | None = None
	match_result: str | None = None
	extraction_method: str | None = None
	prompt_injection_suspected: bool = False
	prompt_injection_indicators: list[str] = field(default_factory=list)
	identity_auto_extracted: bool = False
	identity_source: str = "Provided"  # "Provided" / "Heuristic" / "AI"
	attachments_found: int = 0


# All fields the ingestion gateway can resolve for a candidate profile, in
# one place -- name/contact through to demographic/education/address, filled
# progressively by _fill_profile_gaps (typed -> heuristic -> AI, cheapest
# first, never overriding an already-known value).
_PROFILE_FIELD_NAMES = (
	"first_names",
	"surname",
	"email",
	"mobile",
	"sa_id_number",
	"date_of_birth",
	"gender",
	"languages",
	"physical_address",
	"highest_education_level",
)


@dataclass
class ProfileFields:
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


def ingest_cv(
	*,
	file_bytes: bytes,
	filename: str,
	source_type: str,
	recruitment_source: str | None = None,
	file_url: str | None = None,
	first_names: str | None = None,
	surname: str | None = None,
	email: str | None = None,
	mobile: str | None = None,
	sa_id_number: str | None = None,
) -> IngestionOutcome:
	settings = frappe.get_single("ZA Recruitment Settings")
	ingestion = ingestion_service.create_ingestion(
		source_type=source_type, recruitment_source=recruitment_source, original_filename=filename
	)

	try:
		return _run_pipeline(
			ingestion=ingestion,
			settings=settings,
			file_bytes=file_bytes,
			filename=filename,
			recruitment_source=recruitment_source,
			file_url=file_url,
			first_names=first_names,
			surname=surname,
			email=email,
			mobile=mobile,
			sa_id_number=sa_id_number,
		)
	except frappe.ValidationError as error:
		ingestion_service.reject(ingestion, str(error))
		return IngestionOutcome(status=STATUS_REJECTED, message=str(error), ingestion=ingestion.name)
	except Exception as error:
		frappe.log_error(title="za_recruitment ingestion failed", message=frappe.get_traceback())
		ingestion_service.record_error(ingestion, str(error))
		raise


def _run_pipeline(
	*,
	ingestion,
	settings,
	file_bytes: bytes,
	filename: str,
	recruitment_source: str | None,
	file_url: str | None,
	first_names: str | None,
	surname: str | None,
	email: str | None,
	mobile: str | None,
	sa_id_number: str | None,
) -> IngestionOutcome:
	ingestion_service.set_stage(ingestion, "Validating")
	validate_upload(file_bytes, filename, settings)

	sha256 = sha256_bytes(file_bytes)
	mime_type = mimetypes.guess_type(filename)[0]
	ingestion_service.record_temp_file_metadata(ingestion, file_size=len(file_bytes), mime_type=mime_type, sha256=sha256)

	ingestion_service.set_stage(ingestion, "Duplicate Check")
	existing_cv_name = find_cv_by_hash(sha256)
	if existing_cv_name and settings.reject_exact_duplicate_cv:
		existing_cv = frappe.get_doc("ZA Candidate CV", existing_cv_name)
		existing_cv.received_on = frappe.utils.now_datetime()
		existing_cv.save(ignore_permissions=True)
		ingestion_service.mark_duplicate(ingestion, duplicate_cv_record=existing_cv_name, candidate=existing_cv.candidate)
		return IngestionOutcome(
			status=STATUS_DUPLICATE,
			message="This exact file has already been received and is on record.",
			ingestion=ingestion.name,
			candidate=existing_cv.candidate,
			candidate_cv=existing_cv_name,
		)

	ingestion_service.set_stage(ingestion, "Processing")
	extraction = extract_cv_text(file_bytes, filename)
	ingestion_service.record_extraction(ingestion, method=extraction.method)

	scan = _scan_for_prompt_injection(extraction, settings)
	ingestion_service.record_prompt_injection_scan(ingestion, suspected=scan.suspected, indicators=scan.indicators)

	if scan.suspected and settings.prompt_injection_action == "Reject":
		reason = "Prompt injection indicators detected: " + "; ".join(scan.indicators)
		ingestion_service.reject(ingestion, reason)
		return IngestionOutcome(
			status=STATUS_REJECTED,
			message=reason,
			ingestion=ingestion.name,
			extraction_method=extraction.method,
			prompt_injection_suspected=True,
			prompt_injection_indicators=scan.indicators,
		)

	ingestion_service.set_stage(ingestion, "Candidate Matching")
	profile, identity_source = _fill_profile_gaps(
		extraction.text,
		ProfileFields(
			first_names=first_names, surname=surname, email=email, mobile=mobile, sa_id_number=sa_id_number
		),
	)

	if not (profile.first_names and profile.surname):
		reason = (
			"Could not identify a candidate name -- none was provided, and none could be "
			"determined from the uploaded file's text. Please provide at least a First Name "
			"and Surname."
		)
		# Log what was actually extracted -- without this, "couldn't identify a
		# name" is a black box once the upload's temp File is cleaned up (see
		# upload_cv_manual), since no ZA Candidate CV gets created to hold
		# extraction.text on a rejection.
		frappe.log_error(
			title="za_recruitment: could not identify candidate name",
			message=(
				f"Ingestion: {ingestion.name}\nFilename: {filename}\n"
				f"Extraction method: {extraction.method}\n\n"
				f"--- Per-page text ---\n{_format_page_texts(extraction.page_texts)}\n\n"
				f"--- Full concatenated text ---\n{extraction.text}"
			),
		)
		ingestion_service.reject(ingestion, reason)
		return IngestionOutcome(
			status=STATUS_REJECTED,
			message=reason,
			ingestion=ingestion.name,
			extraction_method=extraction.method,
			prompt_injection_suspected=scan.suspected,
			prompt_injection_indicators=scan.indicators,
		)

	resolution = resolve_or_create_candidate(
		first_names=profile.first_names,
		surname=profile.surname,
		email=profile.email,
		mobile=profile.mobile,
		sa_id_number=profile.sa_id_number,
		date_of_birth=profile.date_of_birth,
		gender=profile.gender,
		languages=profile.languages,
		physical_address=profile.physical_address,
		highest_education_level=profile.highest_education_level,
		settings=settings,
	)

	cv = _create_candidate_cv(
		candidate_name=resolution.candidate.name,
		filename=filename,
		file_url=file_url,
		file_size=len(file_bytes),
		mime_type=mime_type,
		sha256=sha256,
		recruitment_source=recruitment_source,
		ingestion_name=ingestion.name,
		extraction=extraction,
	)

	resolution.candidate.db_set("last_cv_update", today(), update_modified=False)

	attachments_found = _classify_and_record_attachments(
		extraction=extraction, candidate=resolution.candidate, ingestion=ingestion, cv_name=cv.name
	)

	final_status = (
		STATUS_REVIEW_REQUIRED
		if scan.suspected and settings.prompt_injection_action == "Flag For Review"
		else STATUS_ACCEPTED
	)
	ingestion_service.finalize_accepted(
		ingestion,
		candidate=resolution.candidate.name,
		candidate_cv=cv.name,
		match_result=resolution.match_result,
		status=final_status,
	)

	return IngestionOutcome(
		status=final_status,
		message=_summary_message(final_status, resolution.is_new, extraction.method, identity_source),
		ingestion=ingestion.name,
		candidate=resolution.candidate.name,
		candidate_is_new=resolution.is_new,
		candidate_cv=cv.name,
		match_result=resolution.match_result,
		extraction_method=extraction.method,
		prompt_injection_suspected=scan.suspected,
		prompt_injection_indicators=scan.indicators,
		identity_auto_extracted=identity_source != "Provided",
		identity_source=identity_source,
		attachments_found=attachments_found,
	)


def _classify_and_record_attachments(*, extraction, candidate, ingestion, cv_name: str) -> int:
	"""Best-effort: classification bugs must never block the CV/candidate
	acceptance that already succeeded before this runs. Writes an equivalent
	row into *both* candidate.attachments and ingestion.attachments (two
	independent copies by design -- see attachment_classifier.py and the
	field descriptions on both doctypes)."""
	try:
		classified = classify_pages(extraction.page_texts, extraction.page_methods)
	except Exception:
		frappe.log_error(title="za_recruitment attachment classification failed", message=frappe.get_traceback())
		return 0

	if not classified:
		return 0

	for item in classified:
		row = {
			"attachment_type": item.attachment_type,
			"description": item.description,
			"location": "Attached to CV",
			"page_range": item.page_range,
			"candidate_cv": cv_name,
			"ingestion": ingestion.name,
			"match_confidence": item.confidence,
			"auto_classified": 1,
		}
		candidate.append("attachments", dict(row))
		ingestion.append("attachments", dict(row))

	candidate.save(ignore_permissions=True)
	ingestion.save(ignore_permissions=True)
	return len(classified)


def _format_page_texts(page_texts: dict[int, str]) -> str:
	return "\n".join(f"[page {index + 1}]\n{text}\n" for index, text in sorted(page_texts.items()))


def _fill_profile_gaps(text: str, profile: ProfileFields) -> tuple[ProfileFields, str]:
	"""Fills in whatever the caller didn't already provide from the CV's own
	text (design doc §2.1/§3 -- unsolicited CVs must still be accepted; a
	human typing the candidate's details defeats the point of bulk/
	unsolicited intake when the CV plainly states them already). Never
	overrides an explicitly supplied value.

	Two tiers, cheapest first: the free/instant regex heuristic
	(candidate.contact_extraction), then -- if *any* profile field is still
	missing after that, not just the name -- a local/configured LLM
	(candidate.llm_profile_extraction), which costs real latency (seconds,
	on CPU-only inference), so it's a last resort, not a first move, but is
	invoked far more often than a name-only fallback would be, since most
	real CVs will leave *something* (a DOB, an education level, an address)
	for heuristics to miss. Returns which tier (if any) actually supplied
	something, for audit/UI display.
	"""
	if not _has_any_gap(profile):
		return profile, "Provided"

	source = "Provided"

	hints = extract_identity_hints(text)
	profile, filled = _merge_hints(profile, hints)
	if filled:
		source = "Heuristic"

	if _has_any_gap(profile):
		llm_hints = extract_profile_via_llm(text)
		if llm_hints:
			profile, filled = _merge_hints(profile, llm_hints)
			if filled:
				source = "AI"

	return profile, source


def _has_any_gap(profile: ProfileFields) -> bool:
	return any(getattr(profile, name) is None for name in _PROFILE_FIELD_NAMES)


def _merge_hints(profile: ProfileFields, hints) -> tuple[ProfileFields, bool]:
	filled = False
	for name in _PROFILE_FIELD_NAMES:
		current = getattr(profile, name)
		incoming = getattr(hints, name, None)
		if not current and incoming:
			setattr(profile, name, incoming)
			filled = True
	return profile, filled


def _scan_for_prompt_injection(extraction, settings) -> InjectionScanResult:
	if not settings.scan_for_prompt_injection:
		return InjectionScanResult(suspected=False, indicators=[])
	return scan_for_injection_indicators(extraction.text, stripped_span_count=extraction.stripped_span_count)


def _create_candidate_cv(
	*,
	candidate_name: str,
	filename: str,
	file_url: str | None,
	file_size: int,
	mime_type: str | None,
	sha256: str,
	recruitment_source: str | None,
	ingestion_name: str,
	extraction,
):
	existing_current = frappe.get_all(
		"ZA Candidate CV", filters={"candidate": candidate_name, "current": 1}, pluck="name"
	)
	for name in existing_current:
		frappe.db.set_value("ZA Candidate CV", name, {"current": 0, "status": "Historical"})

	cv_version = frappe.db.count("ZA Candidate CV", {"candidate": candidate_name}) + 1

	cv = frappe.get_doc(
		{
			"doctype": "ZA Candidate CV",
			"candidate": candidate_name,
			"cv_version": cv_version,
			"file": file_url,
			"original_filename": filename,
			"received_on": frappe.utils.now_datetime(),
			"source": recruitment_source,
			"ingestion": ingestion_name,
			"file_size": file_size,
			"mime_type": mime_type,
			"sha_256": sha256,
			"status": "Current",
			"current": 1,
			"extracted_text": extraction.text,
		}
	)
	cv.insert(ignore_permissions=True)
	return cv


def _summary_message(status: str, candidate_is_new: bool, extraction_method: str, identity_source: str) -> str:
	who = "new candidate" if candidate_is_new else "existing candidate"
	auto_note = {
		"Heuristic": " (name/contact details auto-detected from the CV text)",
		"AI": " (name/contact details AI-extracted from the CV text)",
	}.get(identity_source, "")
	if status == STATUS_REVIEW_REQUIRED:
		return f"Accepted for {who}{auto_note}, but flagged for human review (prompt-injection indicators found)."
	return f"Accepted for {who}{auto_note}. Text extracted via {extraction_method}."


@frappe.whitelist()
def upload_cv_manual(
	file_url: str,
	first_names: str | None = None,
	surname: str | None = None,
	email: str | None = None,
	mobile: str | None = None,
	sa_id_number: str | None = None,
	recruitment_source: str | None = None,
) -> dict:
	"""Desk-facing entry point behind the "Upload CV" button on the
	ZA Recruitment Ingestion list view. `file_url` points at a private File
	the client has already uploaded via a plain frappe.ui.FileUploader (no
	`method` prop -- see za_recruitment_ingestion_list.js for why). That File
	is deleted here if the gateway doesn't accept the CV, so nothing is left
	orphaned in storage for anything that wasn't kept."""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	# NOTE: deliberately not File.get_content() -- it attempts to text-decode
	# whatever it reads (see core file.py's FILE_ENCODING_OPTIONS loop), and
	# binary content that happens to decode successfully under one of those
	# encodings comes back silently corrupted as a str instead of raising.
	# Reading the raw bytes off disk ourselves is the only reliable way to
	# get back exactly what was uploaded, regardless of file type.
	with open(file_doc.get_full_path(), "rb") as fh:
		file_bytes = fh.read()

	outcome = ingest_cv(
		file_bytes=file_bytes,
		filename=file_doc.file_name,
		source_type="Manual",
		recruitment_source=recruitment_source,
		file_url=file_url,
		first_names=first_names,
		surname=surname,
		email=email,
		mobile=mobile,
		sa_id_number=sa_id_number,
	)

	if outcome.status in (STATUS_ACCEPTED, STATUS_REVIEW_REQUIRED) and outcome.candidate_cv:
		frappe.db.set_value(
			"File",
			file_doc.name,
			{"attached_to_doctype": "ZA Candidate CV", "attached_to_name": outcome.candidate_cv},
		)
	else:
		frappe.delete_doc("File", file_doc.name, ignore_permissions=True, delete_permanently=True)

	return {
		"status": outcome.status,
		"message": outcome.message,
		"ingestion": outcome.ingestion,
		"candidate": outcome.candidate,
		"candidate_is_new": outcome.candidate_is_new,
		"candidate_cv": outcome.candidate_cv,
		"match_result": outcome.match_result,
		"extraction_method": outcome.extraction_method,
		"prompt_injection_suspected": outcome.prompt_injection_suspected,
		"prompt_injection_indicators": outcome.prompt_injection_indicators,
		"identity_auto_extracted": outcome.identity_auto_extracted,
		"identity_source": outcome.identity_source,
		"attachments_found": outcome.attachments_found,
		"filename": file_doc.file_name,
	}
