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

The filesystem.manage catalog was updated the same way in November 2026: its
description now says that organizing a folder is organize_plan followed by
apply_manifest with no manifest (which applies the plan), and it shows both calls
as examples. Only that catalog text and the derived keys changed; recorded
responses were preserved. The organize flow itself is covered by
test_fulfillment.py and test_worker_operator_loop.py.
