import csv
PAYMENTS = 'migration/staging/extract_dec2025_r4_safe/account_invoice_payment_rel.csv'
LINES = 'migration/staging/extract_dec2025_r4_safe/account_invoice_line.csv'
payments = {}
with open(PAYMENTS, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        payments[r['invoice_id']] = payments.get(r['invoice_id'], 0) + 1

has_discount = set()
for_both = set()
with open(LINES, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        if r.get('source_discount_reason_id') and r.get('source_discount_reason_id') not in ('', 'False', '0'):
            has_discount.add(r['invoice_id'])
            if payments.get(r['invoice_id'], 0) > 1:
                for_both.add(r['invoice_id'])
        elif float(r.get('discount', 0) or 0) > 0:
             # Just has discount but no reason?
             pass

multi_payments = [i for i, c in payments.items() if c > 1]
print('With Reason & Multi Payments:', list(for_both)[:5])
print('With Reason:', list(has_discount)[:5])
print('Multi Payments:', multi_payments[:5])
