# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class ZAGeographicAreaCorrection(Document):
	def validate(self):
		if self.status == "Resolved" and self.resolved_area:
			if not self.resolved_by:
				self.resolved_by = frappe.session.user
			if not self.resolved_on:
				self.resolved_on = now_datetime()

			if self.create_alias_on_resolve:
				self.add_alias_to_resolved_area()

	def add_alias_to_resolved_area(self):
		"""Record the raw/OCR'd text as an Alias on the resolved Geographic Area so the
		same bad text matches correctly next time."""
		if not self.raw_text:
			return

		area = frappe.get_doc("ZA Geographic Area", self.resolved_area)
		normalized = self.raw_text.strip().lower()

		already_present = any(
			(row.alias or "").strip().lower() == normalized for row in area.aliases
		)
		if already_present:
			return

		area.append(
			"aliases",
			{
				"alias": self.raw_text.strip(),
				"alias_type": "OCR Correction",
				"weight": 1,
				"active": 1,
			},
		)
		area.save(ignore_permissions=True)
