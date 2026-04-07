SELECT COUNT(*) AS invoices_scope
FROM account_invoice
WHERE date_invoice <= DATE '2025-12-31';

SELECT COUNT(*) AS invoice_lines_scope
FROM account_invoice_line l
JOIN account_invoice i ON i.id = l.invoice_id
WHERE i.date_invoice <= DATE '2025-12-31';

SELECT COUNT(*) AS tax_lines_scope
FROM account_invoice_tax t
JOIN account_invoice i ON i.id = t.invoice_id
WHERE i.date_invoice <= DATE '2025-12-31';

SELECT COUNT(*) AS payment_links_scope
FROM account_invoice_payment_rel p
JOIN account_invoice i ON i.id = p.invoice_id
WHERE i.date_invoice <= DATE '2025-12-31';

SELECT COUNT(*) AS invoices_exceptions
FROM account_invoice
WHERE date_invoice > DATE '2025-12-31';
