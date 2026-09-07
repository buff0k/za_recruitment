# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Exact CV duplicate detection by content hash (design doc §8.2). Frappe
core has no general-purpose SHA-256 helper (`file_manager.get_content_hash`
is MD5, used only for the File doctype's own storage-layer dedup) -- we
compute our own, same as frappe_sign/utils/audit.py does locally."""

from __future__ import annotations

import hashlib

import frappe


def sha256_bytes(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def find_cv_by_hash(sha256: str) -> str | None:
	"""Returns the name of an existing `ZA Candidate CV` with this exact
	content hash, if any."""
	if not sha256:
		return None
	return frappe.db.get_value("ZA Candidate CV", {"sha_256": sha256}, "name")
