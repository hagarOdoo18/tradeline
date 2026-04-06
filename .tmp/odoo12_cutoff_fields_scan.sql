SELECT 'date_invoice' AS field, COUNT(*) AS cnt FROM account_invoice WHERE date_invoice > DATE '2025-12-31'
UNION ALL
SELECT 'date', COUNT(*) FROM account_invoice WHERE "date" > DATE '2025-12-31'
UNION ALL
SELECT 'date_due', COUNT(*) FROM account_invoice WHERE date_due > DATE '2025-12-31'
UNION ALL
SELECT 'modification_date', COUNT(*) FROM account_invoice WHERE modification_date > DATE '2025-12-31'
UNION ALL
SELECT 'e_invoice_date', COUNT(*) FROM account_invoice WHERE e_invoice_date::date > DATE '2025-12-31'
UNION ALL
SELECT 'e_invoice_cancel_date', COUNT(*) FROM account_invoice WHERE e_invoice_cancel_date::date > DATE '2025-12-31'
UNION ALL
SELECT 'create_date', COUNT(*) FROM account_invoice WHERE create_date::date > DATE '2025-12-31'
UNION ALL
SELECT 'write_date', COUNT(*) FROM account_invoice WHERE write_date::date > DATE '2025-12-31'
ORDER BY field;
