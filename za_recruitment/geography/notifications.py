# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import get_url_to_list


def send_unmapped_geographic_area_reminder():
	"""Daily scheduler job.

	If ZA Recruitment Settings > Branch Geographic Areas > "Enable Unmapped Area
	Reminders" is checked, find every active ZA Geographic Area that has no row in
	the settings' `branch_geographic_areas` child table, and email a summary to
	every user holding the HR Manager role. A Geographic Area with no Branch mapped
	to it will never surface candidates for local/branch recruitment.
	"""
	settings = frappe.get_single("ZA Recruitment Settings")

	if not settings.enable_unmapped_area_reminders:
		return

	mapped_areas = {row.geographic_area for row in settings.branch_geographic_areas if row.geographic_area}

	unmapped_areas = frappe.get_all(
		"ZA Geographic Area",
		filters={"active": 1},
		fields=["name", "geographic_area_name"],
		order_by="geographic_area_name asc",
	)
	unmapped_areas = [area for area in unmapped_areas if area.name not in mapped_areas]

	if not unmapped_areas:
		return

	recipients = get_hr_manager_emails()
	if not recipients:
		return

	frappe.sendmail(
		recipients=recipients,
		subject=f"ZA Recruitment: {len(unmapped_areas)} Geographic Area(s) not mapped to a Branch",
		message=build_reminder_message(unmapped_areas),
		reference_doctype="ZA Recruitment Settings",
		reference_name="ZA Recruitment Settings",
	)


def get_hr_manager_emails():
	users = frappe.get_all(
		"Has Role",
		filters={"role": "HR Manager", "parenttype": "User"},
		pluck="parent",
	)
	if not users:
		return []

	return frappe.get_all(
		"User",
		filters={"name": ["in", users], "enabled": 1},
		pluck="email",
	)


def build_reminder_message(unmapped_areas):
	rows = "".join(f"<li>{frappe.utils.escape_html(area.geographic_area_name)}</li>" for area in unmapped_areas)
	settings_url = get_url_to_list("ZA Recruitment Settings")

	return f"""
		<p>The following active Geographic Areas have no Branch mapped to them in
		ZA Recruitment Settings &gt; Branch Geographic Areas. Candidates matched to
		these areas will not surface for any Branch's local recruitment until a
		mapping is added.</p>
		<ul>{rows}</ul>
		<p><a href="{settings_url}">Open ZA Recruitment Settings</a></p>
	"""
