# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Thin, generic OpenAI-compatible chat client, configured entirely from
`ZA Recruitment Settings` > AI Configuration (`ai_endpoint`, `ai_model`,
`ai_api_key`, `ai_timeout_seconds`, `ai_retry_count`) -- works against
OpenAI/ChatGPT, Azure OpenAI, a local Ollama server, or any other
OpenAI-compatible endpoint without code changes, per that tab's design.

Callers get `None` back (never an exception) when AI processing is
disabled/unconfigured or when the call itself fails (timeout, connection
refused, malformed response) -- every AI-assisted step in this app is a
best-effort enhancement over a working deterministic fallback, never a hard
dependency. Failures are logged via `frappe.log_error` for visibility.
"""

from __future__ import annotations

import frappe
from openai import OpenAI

# Smaller models (confirmed against a real response from llama3.2:3b)
# sometimes emit the literal string "null" -- or "none"/"n/a"/etc. -- instead
# of a real JSON null for a field that has no value. Left unsanitized, a
# placeholder like that can end up stored as if it were real data (a real
# response very nearly became a candidate's email address, tripping Frappe's
# Email fieldtype validation with "null is not a valid Email Address").
# Centralized here rather than per-caller since it's a property of the model/
# API layer, not any one tool schema.
_NULL_LIKE_VALUES = {"null", "none", "n/a", "na", "unknown", "not found", "not provided", "not specified", "-", ""}


def _sanitize_value(value):
	if not isinstance(value, str):
		return value
	return None if value.strip().lower() in _NULL_LIKE_VALUES else value


def get_client() -> tuple[OpenAI, str] | None:
	"""Returns (client, model_name), or None if AI processing isn't enabled
	or configured. `settings.ai_api_key` is optional -- a local Ollama
	server needs none."""
	settings = frappe.get_single("ZA Recruitment Settings")

	if not settings.enable_ai_processing:
		return None
	if not settings.ai_endpoint or not settings.ai_model:
		return None

	api_key = "not-required"
	if settings.ai_api_key:
		try:
			api_key = settings.get_password("ai_api_key")
		except Exception:
			pass

	timeout = settings.ai_timeout_seconds or 30
	client = OpenAI(base_url=settings.ai_endpoint, api_key=api_key, timeout=timeout)
	return client, settings.ai_model


def call_tool(
	*,
	system_prompt: str,
	user_content: str,
	tool_name: str,
	tool_description: str,
	parameters_schema: dict,
) -> dict | None:
	"""Forces the model to respond via a single named tool call, so the
	result is structured JSON rather than free text to parse -- validated to
	work reliably with `llama3.2:3b` against real, messy OCR text. Returns
	the parsed tool-call arguments dict, or None on any failure (disabled,
	unreachable, timeout, malformed response)."""
	import json

	config = get_client()
	if config is None:
		return None
	client, model = config

	settings = frappe.get_single("ZA Recruitment Settings")
	retry_count = settings.ai_retry_count if settings.ai_retry_count is not None else 1
	attempts = max(1, retry_count)

	tools = [
		{
			"type": "function",
			"function": {
				"name": tool_name,
				"description": tool_description,
				"parameters": parameters_schema,
			},
		}
	]

	last_error = None
	for _ in range(attempts):
		try:
			response = client.chat.completions.create(
				model=model,
				messages=[
					{"role": "system", "content": system_prompt},
					{"role": "user", "content": user_content},
				],
				tools=tools,
				tool_choice={"type": "function", "function": {"name": tool_name}},
				temperature=settings.ai_temperature or 0,
			)
			message = response.choices[0].message

			if settings.store_prompt:
				frappe.logger("za_recruitment.ai").info(f"prompt: {system_prompt}\n\n{user_content}")
			if settings.store_raw_ai_response:
				frappe.logger("za_recruitment.ai").info(f"response: {message}")

			if not message.tool_calls:
				return None
			parsed = json.loads(message.tool_calls[0].function.arguments)
			return {key: _sanitize_value(value) for key, value in parsed.items()}
		except Exception as error:
			last_error = error
			continue

	frappe.log_error(
		title="za_recruitment AI call failed",
		message=f"tool={tool_name} model={model} attempts={attempts}\n{last_error}",
	)
	return None
