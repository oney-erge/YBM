# Recorded scenario fixtures

These fixtures replay stored model responses and do not call a live model.
Prompt matching remains strict, including the enabled tool catalog.

The document.manage catalog was updated to include compare_documents in October
2026. Only its description, operation list, example, and derived fixture keys
were changed. Original system prompts and recorded responses were preserved.
This was a catalog maintenance update, not a live model re-record. Passing these
scenarios does not establish that a live model will select the new operation.
The new comparison flow has separate deterministic worker and browser integration
coverage in test_projects_automation.py and frontend/integration/projects.spec.ts.
