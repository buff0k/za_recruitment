// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.listview_settings["ZA Recruitment Ingestion"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Upload CV"), () => {
			show_upload_cv_dialog(listview);
		});
	},
};

function show_upload_cv_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Upload CV (Manual)"),
		fields: [
			{
				fieldname: "help",
				fieldtype: "HTML",
				options: `<p>${__(
					"For testing the ingestion pipeline. Candidate details below are optional hints used to match against an existing candidate (by South African ID, email, or mobile) -- a new candidate is created if nothing matches."
				)}</p>`,
			},
			{ fieldname: "first_names", fieldtype: "Data", label: __("First Names") },
			{ fieldname: "surname", fieldtype: "Data", label: __("Surname") },
			{ fieldname: "column_break_1", fieldtype: "Column Break" },
			{ fieldname: "email", fieldtype: "Data", label: __("Email"), options: "Email" },
			{ fieldname: "mobile", fieldtype: "Data", label: __("Mobile") },
			{ fieldname: "section_break_1", fieldtype: "Section Break" },
			{ fieldname: "sa_id_number", fieldtype: "Data", label: __("South African ID Number") },
			{
				fieldname: "recruitment_source",
				fieldtype: "Link",
				label: __("Source"),
				options: "Job Applicant Source",
			},
		],
		primary_action_label: __("Choose File(s) & Upload"),
		primary_action: (values) => {
			dialog.hide();
			open_file_uploader(listview, values);
		},
	});
	dialog.show();
}

function open_file_uploader(listview, hints) {
	new frappe.ui.FileUploader({
		allow_multiple: true,
		as_private: true,
		folder: "Home/Attachments",
		on_success: (file_doc) => {
			process_uploaded_cv(listview, file_doc, hints);
		},
	});
}

function process_uploaded_cv(listview, file_doc, hints) {
	frappe.call({
		method: "za_recruitment.ingestion.gateway.upload_cv_manual",
		args: {
			file_url: file_doc.file_url,
			first_names: hints.first_names,
			surname: hints.surname,
			email: hints.email,
			mobile: hints.mobile,
			sa_id_number: hints.sa_id_number,
			recruitment_source: hints.recruitment_source,
		},
		freeze: true,
		freeze_message: __("Processing {0}... (may take up to a minute if AI-assisted identity extraction is needed)", [
			file_doc.file_name,
		]),
		callback: (r) => {
			show_result(r.message);
			listview.refresh();
		},
		error: () => {
			frappe.msgprint({
				title: __("Processing Failed"),
				indicator: "red",
				message: __("{0} could not be processed. See the Error Log for details.", [file_doc.file_name]),
			});
			listview.refresh();
		},
	});
}

const STATUS_INDICATOR = {
	Accepted: "green",
	"Review Required": "orange",
	Duplicate: "blue",
	Rejected: "red",
	Failed: "red",
};

function show_result(result) {
	const rows = [
		[__("File"), frappe.utils.escape_html(result.filename)],
		[__("Status"), `<span class="indicator-pill ${STATUS_INDICATOR[result.status] || "gray"}">${result.status}</span>`],
		[__("Message"), frappe.utils.escape_html(result.message || "")],
	];

	if (result.ingestion) {
		rows.push([__("Ingestion"), frappe.utils.get_form_link("ZA Recruitment Ingestion", result.ingestion, true)]);
	}
	if (result.candidate) {
		const new_tag = result.candidate_is_new ? ` <span class="text-muted">(${__("new")})</span>` : "";
		const source_labels = {
			Heuristic: __("name/contact auto-detected from CV text"),
			AI: __("name/contact AI-extracted from CV text"),
		};
		const auto_tag = source_labels[result.identity_source]
			? ` <span class="text-muted">(${source_labels[result.identity_source]})</span>`
			: "";
		rows.push([
			__("Candidate"),
			frappe.utils.get_form_link("ZA Candidate", result.candidate, true) + new_tag + auto_tag,
		]);
	}
	if (result.candidate_cv) {
		rows.push([__("Candidate CV"), frappe.utils.get_form_link("ZA Candidate CV", result.candidate_cv, true)]);
	}
	if (result.match_result) {
		rows.push([__("Match Result"), frappe.utils.escape_html(result.match_result)]);
	}
	if (result.extraction_method) {
		rows.push([__("Extraction Method"), frappe.utils.escape_html(result.extraction_method)]);
	}
	if (result.attachments_found) {
		rows.push([
			__("Supporting Documents"),
			__("{0} classified — see Attachments on the Candidate record", [result.attachments_found]),
		]);
	}
	if (result.prompt_injection_suspected) {
		rows.push([
			__("Prompt Injection"),
			`<span class="indicator-pill orange">${__("Suspected")}</span><br>` +
				(result.prompt_injection_indicators || [])
					.map((indicator) => frappe.utils.escape_html(indicator))
					.join("<br>"),
		]);
	}

	const table_html = `
		<table class="table table-bordered">
			${rows.map(([label, value]) => `<tr><td><strong>${label}</strong></td><td>${value}</td></tr>`).join("")}
		</table>
	`;

	frappe.msgprint({
		title: __("Upload Result"),
		indicator: STATUS_INDICATOR[result.status] || "gray",
		message: table_html,
		wide: true,
	});
}
