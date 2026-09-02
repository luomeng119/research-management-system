CREATE TABLE expense_reimbursement (id INTEGER PRIMARY KEY, reimbursement_no TEXT, title TEXT, total_amount TEXT, status TEXT, is_paid TEXT, documents TEXT);
INSERT INTO expense_reimbursement VALUES
  (30, 'R-OK', 'Valid expense', '120.20', '草稿', '0', '["uploads/invoice-a.txt","uploads/invoice-copy.txt"]'),
  (31, 'R-BAD-MONEY', 'Bad money', '12.xx', '草稿', '0', '["uploads/invoice-a.txt"]'),
  (32, 'R-BAD-JSON', 'Bad JSON', '12.20', '草稿', '0', '{bad-json');
CREATE TABLE expense_invoice (id INTEGER PRIMARY KEY, reimbursement_id INTEGER, invoice_no TEXT, date TEXT, amount TEXT, tax_amount TEXT, price_ex_tax TEXT, file_path TEXT, matched_payment_ids TEXT, created_at TEXT);
INSERT INTO expense_invoice VALUES (40, 30, 'INV-OK', '2026-09-01', '120.20', '10.00', '110.20', 'uploads/missing.txt', '[50]', '2026-09-01 10:00:00');
CREATE TABLE expense_payment (id INTEGER PRIMARY KEY, reimbursement_id INTEGER, payment_no TEXT, amount TEXT, pay_date TEXT, file_path TEXT, matched_invoice_ids TEXT, created_at TEXT);
INSERT INTO expense_payment VALUES (50, 30, 'PAY-OK', '120.20', '2026-09-02', 'uploads/invoice-a.txt', '[40]', '2026-09-01 10:00:00');
CREATE TABLE expense_invoice_item (id INTEGER PRIMARY KEY, invoice_id INTEGER, seq INTEGER, name TEXT, quantity TEXT, unit_price TEXT, amount TEXT, tax_amount TEXT);
INSERT INTO expense_invoice_item VALUES (60, 40, 1, 'Valid item', '1.0000', '110.2000', '110.20', '10.00');
