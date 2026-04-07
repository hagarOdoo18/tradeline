SELECT 'account_move_invoice_date_gt_cutoff' AS metric, COUNT(*) AS cnt
FROM account_move
WHERE invoice_date > DATE '2025-12-31'
  AND move_type IN ('out_invoice','out_refund','in_invoice','in_refund');

SELECT 'account_move_date_gt_cutoff' AS metric, COUNT(*) AS cnt
FROM account_move
WHERE "date" > DATE '2025-12-31'
  AND move_type IN ('out_invoice','out_refund','in_invoice','in_refund');

SELECT 'account_invoice_date_invoice_gt_cutoff' AS metric, COUNT(*) AS cnt
FROM account_invoice
WHERE date_invoice > DATE '2025-12-31';
