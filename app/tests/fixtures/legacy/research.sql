CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT, role TEXT, name TEXT, status TEXT, directory_permissions TEXT);
INSERT INTO users VALUES
  (7, 'hashed', '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', '管理员', 'Hashed User', 'active', '{}'),
  (8, 'plain', 'plain-secret', '用户', 'Plain User', 'active', '{}');
CREATE TABLE projects (id INTEGER PRIMARY KEY, project_id TEXT, name TEXT, start_date TEXT, status TEXT);
INSERT INTO projects VALUES
  (11, 'P-001', 'Valid project', '2026-09-01', 'ACTIVE'),
  (12, 'P-BAD', 'Bad date project', '2026-99-99', 'ACTIVE');
CREATE TABLE security_projects (id INTEGER PRIMARY KEY, project_id TEXT, name TEXT);
CREATE TABLE crypto_projects (id INTEGER PRIMARY KEY, project_id TEXT, name TEXT);
CREATE TABLE equipment (id INTEGER PRIMARY KEY, equipment_id TEXT, name TEXT, category TEXT, price TEXT, related_files TEXT, created_at TEXT);
INSERT INTO equipment VALUES (13, 'EQ-001', 'Valid equipment', 'instrument', '120.50', '["uploads/invoice-a.txt"]', '2026-09-01 10:00:00');
CREATE TABLE standards (id INTEGER PRIMARY KEY, doc_id TEXT, name TEXT, category TEXT, file_path TEXT, upload_time TEXT);
INSERT INTO standards VALUES (14, 'STD-001', 'Valid standard', 'standard', NULL, '2026-09-01 10:00:00');
CREATE TABLE experts (id INTEGER PRIMARY KEY, expert_id TEXT, name TEXT, unit TEXT, created_at TEXT, updated_at TEXT);
INSERT INTO experts VALUES (15, 'EX-001', 'Valid expert', 'Institute', '2026-09-01 10:00:00', '2026-09-01 10:00:00');
CREATE TABLE doc_templates (id INTEGER PRIMARY KEY, template_id TEXT, name TEXT, category TEXT, file_path TEXT, uploader TEXT, created_at TEXT);
INSERT INTO doc_templates VALUES (16, 'TPL-001', 'Valid template', '财务模板', NULL, 'hashed', '2026-09-01 10:00:00');
CREATE TABLE project_documents (id INTEGER PRIMARY KEY, doc_id TEXT, project_id TEXT, name TEXT, file_path TEXT);
CREATE TABLE document_versions (id INTEGER PRIMARY KEY, version_id TEXT, doc_id TEXT, version_number TEXT, file_path TEXT);
CREATE TABLE expert_groups (id INTEGER PRIMARY KEY, group_id TEXT, meeting_name TEXT, creator TEXT, created_at TEXT, updated_at TEXT);
INSERT INTO expert_groups VALUES (17, 'EG-001', 'Review group', 'hashed', '2026-09-01 10:00:00', '2026-09-01 10:00:00');
CREATE TABLE expert_group_members (id INTEGER PRIMARY KEY, group_id TEXT, expert_id TEXT, selected_by TEXT, selected_at TEXT);
INSERT INTO expert_group_members VALUES (18, 'EG-001', 'EX-001', 'hashed', '2026-09-01 10:00:00');
CREATE TABLE llm_models (id INTEGER PRIMARY KEY, name TEXT, model_type TEXT, file_path TEXT, api_key TEXT, created_at TEXT, updated_at TEXT);
INSERT INTO llm_models VALUES (19, 'old model', 'corrector', '/private/model.gguf', 'secret-api-key', '2026-09-01 10:00:00', '2026-09-01 10:00:00');
CREATE TABLE inference_server_status (id INTEGER PRIMARY KEY, server_status TEXT);
INSERT INTO inference_server_status VALUES (1, 'running');
