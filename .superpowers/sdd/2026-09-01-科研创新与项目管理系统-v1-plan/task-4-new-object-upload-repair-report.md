# T04 repair report — atomic first upload for retained resource objects

## Result

- Added `FileService.upload_new_object(...)`. It stages and validates bytes first,
  then calls `create_metadata(active_connection)`, locks and validates the newly
  created business object, creates the first stored file/version/link, moves the
  file, records the audit event, and commits in one transaction.
- Existing `FileService.upload(...)` signature and its pre-existing object
  validation remain unchanged. Both paths use the same private file-write flow.
- The callback receives the active SQLAlchemy connection and is documented as
  forbidden from starting or committing its own transaction.
- `TEMPLATE` now resolves to `reference_template_items.template_id`; structured
  argumentation `doc_templates` is no longer a controlled-file object target.
- `STANDARD` and `TEMPLATE` with `status = 'ARCHIVED'` reject upload, new-version,
  and archive mutations with `OBJECT_READ_ONLY`.
- Object-table reflection during a write transaction now uses that active
  connection, avoiding a separate reflection connection that can interfere with
  SQLite's single shared test connection.

## TDD evidence

- RED: `.venv/bin/python -m pytest app/tests/test_file_service.py::test_upload_new_object_creates_standard_metadata_and_first_file_atomically -q`
  returned `1 failed`; expected failure was `AttributeError: 'FileService' object
  has no attribute 'upload_new_object'`.
- GREEN: the same focused test returned `1 passed` after the narrow seam was
  implemented.
- Focused cleanup/read-only coverage:
  `.venv/bin/python -m pytest app/tests/test_file_service.py -k 'upload_new_object or archived_retained_resource' -q`
  returned `8 passed, 32 deselected`.
- Full file-service regression:
  `.venv/bin/python -m pytest app/tests/test_file_service.py -q` returned
  `37 passed, 3 skipped`.
- Relevant PostgreSQL-selected coverage:
  `.venv/bin/python -m pytest app/tests/test_file_service.py -k postgres -q`
  returned `3 skipped, 37 deselected`; `TEST_DATABASE_URL` was absent, so no real
  PostgreSQL contract runner was available in this workspace.
- `git diff --check` passed.

## Failure evidence

The focused SQLite tests inject metadata-creator, post-creator write-lock, audit,
`os.replace`, and transaction-commit failures. Each checks the actual business
metadata table, all three file metadata tables, staged/final filesystem files, and
where applicable confirms no residue remains.

## Remaining risk

- Real PostgreSQL execution remains required once the isolated
  `TEST_DATABASE_URL` runner is available; the existing selected tests were skipped
  only because that environment variable is unset.
- The accepted T04 boundary remains: process termination after `os.replace` but
  before transaction cleanup/commit can leave a short-lived orphan final file;
  reconciliation belongs to the later T12 file/DB audit rather than this repair.
