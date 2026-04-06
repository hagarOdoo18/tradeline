SELECT 'date_invoice_gt_2025_12_31' AS metric, COUNT(*) AS cnt FROM account_invoice WHERE date_invoice > DATE '2025-12-31';
SELECT 'date_gt_2025_12_31' AS metric, COUNT(*) AS cnt FROM account_invoice WHERE "date" > DATE '2025-12-31';
SELECT 'create_date_gt_2025_12_31' AS metric, COUNT(*) AS cnt FROM account_invoice WHERE create_date::date > DATE '2025-12-31';
SELECT 'write_date_gt_2025_12_31' AS metric, COUNT(*) AS cnt FROM account_invoice WHERE write_date::date > DATE '2025-12-31';
