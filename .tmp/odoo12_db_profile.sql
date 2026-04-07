WITH base AS (
  SELECT
    current_database() AS db,
    COUNT(*) AS invoices,
    COUNT(*) FILTER (WHERE date_invoice IS NULL) AS null_date_invoice,
    MIN(date_invoice) AS min_date_invoice,
    MAX(date_invoice) AS max_date_invoice,
    COUNT(*) FILTER (WHERE date_invoice <= DATE '2025-12-31') AS invoices_le_cutoff,
    COUNT(*) FILTER (WHERE date_invoice > DATE '2025-12-31') AS invoices_gt_cutoff,
    MAX(create_date) AS max_create_date,
    MAX(write_date) AS max_write_date,
    COUNT(DISTINCT number) FILTER (WHERE number IS NOT NULL AND number <> '') AS distinct_numbers,
    COUNT(*) FILTER (WHERE number IS NULL OR number = '') AS blank_number_rows,
    MAX(id) AS max_invoice_id
  FROM account_invoice
)
SELECT * FROM base;

SELECT current_database() AS db,
       COUNT(*) AS invoice_lines,
       COUNT(*) FILTER (WHERE invoice_id IS NULL) AS lines_without_invoice_id,
       MAX(id) AS max_line_id
FROM account_invoice_line;

SELECT current_database() AS db,
       COUNT(*) AS tax_lines,
       MAX(id) AS max_tax_id
FROM account_invoice_tax;

SELECT current_database() AS db,
       COUNT(*) AS payment_links
FROM account_invoice_payment_rel;

SELECT current_database() AS db,
       COUNT(*) AS invoice_attachments,
       COALESCE(SUM(file_size),0) AS attachment_bytes
FROM ir_attachment
WHERE res_model='account.invoice';

SELECT current_database() AS db,
       EXTRACT(YEAR FROM date_invoice)::int AS y,
       COUNT(*) AS c
FROM account_invoice
WHERE date_invoice IS NOT NULL
GROUP BY EXTRACT(YEAR FROM date_invoice)
ORDER BY y;
