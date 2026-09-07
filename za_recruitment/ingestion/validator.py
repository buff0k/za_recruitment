# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Pre-ingestion validation against `ZA Recruitment Settings` -- runs before
any hashing/dedup/extraction work. Server-side and authoritative regardless
of whatever client-side restrictions a Desk dialog or web form may also
apply (non-negotiable: server-side validation must not be optional)."""

from __future__ import annotations

import os

import frappe


def validate_upload(file_bytes: bytes, filename: str, settings) -> None:
	"""Raises frappe.ValidationError with a clear reason on failure."""
	if not file_bytes:
		frappe.throw(frappe._("The uploaded file is empty."))

	# `or 10` would silently treat an intentionally-set 0 the same as unset
	# (0 is falsy) -- check for None explicitly so a real 0 MB limit actually
	# rejects everything rather than quietly falling back to the default.
	max_size_mb = settings.maximum_upload_size_mb if settings.maximum_upload_size_mb is not None else 10
	max_size_bytes = max_size_mb * 1024 * 1024
	if len(file_bytes) > max_size_bytes:
		frappe.throw(frappe._("File exceeds the maximum upload size of {0} MB.").format(max_size_mb))

	allowed_types = _parse_allowed_types(settings.allowed_file_types)
	extension = os.path.splitext(filename or "")[1].lower()
	if allowed_types and extension not in allowed_types:
		frappe.throw(
			frappe._("File type {0} is not allowed. Allowed types: {1}").format(
				extension or "(none)", ", ".join(sorted(allowed_types))
			)
		)


def _parse_allowed_types(raw: str | None) -> set[str]:
	if not raw:
		return set()
	return {
		part.strip().lower() if part.strip().startswith(".") else f".{part.strip().lower()}"
		for part in raw.split(",")
		if part.strip()
	}
