CREATE TABLE generic_tables (id INTEGER PRIMARY KEY, table_id TEXT, name TEXT, creator TEXT, created_at TEXT, updated_at TEXT, current_version_id TEXT);
INSERT INTO generic_tables VALUES (21, 'GT-001', 'Valid generic table', 'hashed', '2026-09-01 10:00:00', '2026-09-01 10:00:00', 'GTV-001');
CREATE TABLE generic_table_versions (id INTEGER PRIMARY KEY, version_id TEXT, table_id TEXT, version_number INTEGER, create_method TEXT, row_count INTEGER, page_size INTEGER, creator TEXT, created_at TEXT, is_locked TEXT);
INSERT INTO generic_table_versions VALUES (22, 'GTV-001', 'GT-001', 1, 'manual', 2, 20, 'hashed', '2026-09-01 10:00:00', '1');
CREATE TABLE generic_table_columns (id INTEGER PRIMARY KEY, version_id TEXT, col_key TEXT, col_name TEXT, col_type TEXT, col_index INTEGER, col_width INTEGER, col_align TEXT, col_summary TEXT, col_options TEXT, created_at TEXT);
INSERT INTO generic_table_columns VALUES (23, 'GTV-001', 'name', 'Name', 'text', 0, 120, 'left', '', '["A","B"]', '2026-09-01 10:00:00');
CREATE TABLE generic_table_data (id INTEGER PRIMARY KEY, version_id TEXT, row_key TEXT, row_index INTEGER, row_data TEXT, row_color TEXT, created_at TEXT, updated_at TEXT);
INSERT INTO generic_table_data VALUES
  (24, 'GTV-001', 'R1', 1, '{"name":"valid"}', '', '2026-09-01 10:00:00', '2026-09-01 10:00:00'),
  (25, 'GTV-001', 'R2', 2, '{bad-json', '', '2026-09-01 10:00:00', '2026-09-01 10:00:00');
