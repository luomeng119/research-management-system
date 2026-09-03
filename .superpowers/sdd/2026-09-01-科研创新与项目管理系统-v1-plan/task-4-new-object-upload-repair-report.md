# T04 repair report — atomic first upload for retained resource objects

## Result

- Added `FileService.upload_new_object(...)`. It stages and validates bytes first,
  then calls `create_metadata(writer)`, locks and validates the newly created
  business object, creates the first stored file/version/link, moves the file,
  records the audit event, and commits in one transaction.
- Existing `FileService.upload(...)` signature and its pre-existing object
  validation remain unchanged. Both paths use the same private file-write flow.
- The callback receives a restricted active-transaction SQLAlchemy writer. It
  supports only insert-only `execute` and read-only `in_transaction`. `execute`
  accepts an SQLAlchemy `Insert` statement only, returns `None`, and rejects
  text, DDL, select, update, delete, and transaction statements. It therefore
  cannot return a `CursorResult`/`ScalarResult` with a connection/context/engine
  escape. It also explicitly rejects transaction/lifecycle control (`commit`,
  `rollback`, `begin`, `begin_nested`, `close`, `invalidate`, `detach`, and
  context-manager entry/exit), and exposes no raw connection or engine attribute.
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
- Review-repair RED:
  `.venv/bin/python -m pytest app/tests/test_file_service.py::test_upload_new_object_rejects_callback_transaction_control_without_residue -q`
  returned `4 failed`; each expected failure showed that raw callback transaction
  control was not explicitly rejected.
- Review-repair GREEN: the same test returned `4 passed` after the restricted
  writer replaced the raw callback connection.
- Second-review RED:
  `.venv/bin/python -m pytest app/tests/test_file_service.py::test_upload_new_object_writer_rejects_result_and_sql_transaction_escapes -q`
  returned `2 failed`: callback `CursorResult.connection` and `text('COMMIT')`
  were still reachable.
- Second-review GREEN: the same attack test returned `2 passed` after the writer
  was narrowed to non-returning SQLAlchemy-`Insert` execution.
- PostgreSQL stale-lock RED: the new `archive` race failed because `archive` did
  not first complete the ordinary ACTIVE precheck before its locked recheck.
  Adding the compatible precheck made the full parameterized race green.
- Focused cleanup/read-only coverage:
  `.venv/bin/python -m pytest app/tests/test_file_service.py -k 'upload_new_object or archived_retained_resource' -q`
  returned `18 passed, 36 deselected`.
- Full file-service regression:
  `.venv/bin/python -m pytest app/tests/test_file_service.py -q` returned
  `47 passed, 7 skipped`.
- Real isolated PostgreSQL T04 contract:
  `scripts/test_postgres.sh postgresql://localhost/rm_v1_t04` completed. Its T04
  selection returned `7 passed, 47 deselected`, including the same-`doc_id`
  first-standard race: exactly one business row, file, V1, object link, and audit
  record survived; the losing staged file was removed. The parameterized archive
  lock race holds an uncommitted ARCHIVED standard row under `FOR UPDATE`; upload,
  new-version, and archive workers each first observe the prior ACTIVE snapshot,
  block at their locked recheck, then return `OBJECT_READ_ONLY` after the archive
  transaction commits. Each asserts no additional file/version/link/audit/disk
  residue.
- `git diff --check` passed.

## Failure evidence

The focused SQLite tests inject metadata-creator, post-creator write-lock query,
`create_file`, `create_version`, `link_object`, audit, `os.replace`, and
transaction-commit failures. They also attempt callback `commit`, `rollback`,
`begin`, `begin_nested`, `CursorResult.connection.commit()`, and SQL text
`COMMIT`. Each checks the actual business metadata table, all three file metadata
tables, audit absence where the audit boundary was not reached, and staged/final
filesystem files; no residue remains.

The archived STANDARD and TEMPLATE test explicitly exercises all three mutation
paths after archival: fresh upload, new version, and file archive.

## Remaining risk

- The accepted T04 boundary remains: process termination after `os.replace` but
  before transaction cleanup/commit can leave a short-lived orphan final file;
  reconciliation belongs to the later T12 file/DB audit rather than this repair.
