#!/usr/bin/env python3
"""Export the monthly Invoice-Analysis vs Full-History "current" audit sheet.

Runs ``audit_current_vs_invoice_analysis.sql`` against the Odoo Postgres
database and writes an .xlsx (falls back to .csv if no Excel writer is
available). One row per month, with the columns needed to reconcile the
Full-History "current" lines against the Invoices Analysis report.

Two ways to run it
------------------
1) Standalone (uses libpq env vars / CLI flags)::

       python export_monthly_subtotals.py \
           --dbname tradline --host localhost --user odoo --password ****** \
           --output monthly_subtotals.xlsx

   Connection defaults come from the standard PG* env vars
   (PGDATABASE/PGHOST/PGPORT/PGUSER/PGPASSWORD) when flags are omitted.

2) Inside ``odoo shell`` (reuses the live cursor, no creds needed)::

       odoo shell -d tradline
       >>> exec(open('legacy_current_product_comparison/tools/export_monthly_subtotals.py').read())
       # uses the existing `env` cursor and writes ./monthly_subtotals.xlsx
"""

import argparse
import csv
import os
import sys
from datetime import datetime

SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "audit_current_vs_invoice_analysis.sql")


def _load_sql():
    with open(SQL_FILE, "r", encoding="utf-8") as fh:
        return fh.read()


def _write_xlsx(path, columns, rows):
    """Write rows to an .xlsx; return True on success, False if no writer."""
    try:
        import xlsxwriter  # bundled with Odoo (report_xlsx / base)
    except ImportError:
        xlsxwriter = None

    if xlsxwriter is not None:
        wb = xlsxwriter.Workbook(path)
        ws = wb.add_worksheet("Monthly Subtotals")
        header_fmt = wb.add_format({"bold": True, "bg_color": "#DDDDDD", "border": 1})
        num_fmt = wb.add_format({"num_format": "#,##0.00"})
        qty_fmt = wb.add_format({"num_format": "#,##0.000"})
        for col, name in enumerate(columns):
            ws.write(0, col, name, header_fmt)
        for r, row in enumerate(rows, start=1):
            for c, value in enumerate(row):
                if c == 0:
                    ws.write(r, c, value)
                elif "qty" in columns[c]:
                    ws.write_number(r, c, float(value or 0), qty_fmt)
                else:
                    ws.write_number(r, c, float(value or 0), num_fmt)
        # Totals row
        total_row = len(rows) + 1
        ws.write(total_row, 0, "TOTAL", header_fmt)
        for c in range(1, len(columns)):
            col_letter = xlsxwriter.utility.xl_col_to_name(c)
            ws.write_formula(
                total_row, c,
                f"=SUM({col_letter}2:{col_letter}{len(rows) + 1})",
                num_fmt if "qty" not in columns[c] else qty_fmt,
            )
        ws.freeze_panes(1, 1)
        ws.set_column(0, 0, 10)
        ws.set_column(1, len(columns) - 1, 18)
        wb.close()
        return True

    try:
        import openpyxl
    except ImportError:
        return False

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monthly Subtotals"
    ws.append(list(columns))
    for row in rows:
        ws.append(list(row))
    wb.save(path)
    return True


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerows(rows)


def export(cr, output):
    cr.execute(_load_sql())
    columns = [d[0] for d in cr.description]
    rows = cr.fetchall()

    if output.lower().endswith(".xlsx") and _write_xlsx(output, columns, rows):
        written = output
    else:
        written = output if output.lower().endswith(".csv") else output.rsplit(".", 1)[0] + ".csv"
        _write_csv(written, columns, rows)

    print(f"Wrote {len(rows)} month(s) to {written}")
    # Echo to console too, so it is useful even without opening the file.
    print("\t".join(columns))
    for row in rows:
        print("\t".join("" if v is None else str(v) for v in row))
    return written


def _standalone():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbname", default=os.environ.get("PGDATABASE"))
    parser.add_argument("--host", default=os.environ.get("PGHOST", "localhost"))
    parser.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    parser.add_argument("--user", default=os.environ.get("PGUSER"))
    parser.add_argument("--password", default=os.environ.get("PGPASSWORD"))
    parser.add_argument(
        "--output",
        default="monthly_subtotals_%s.xlsx" % datetime.now().strftime("%Y%m%d"),
    )
    args = parser.parse_args()

    if not args.dbname:
        parser.error("database name required (--dbname or PGDATABASE)")

    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for standalone mode: pip install psycopg2-binary")

    conn = psycopg2.connect(
        dbname=args.dbname, host=args.host, port=args.port,
        user=args.user, password=args.password,
    )
    try:
        with conn.cursor() as cr:
            export(cr, args.output)
    finally:
        conn.close()


# When exec()'d inside `odoo shell`, an `env` global is present -> reuse it.
if "env" in globals():
    export(env.cr, os.path.join(os.getcwd(), "monthly_subtotals.xlsx"))  # noqa: F821
elif __name__ == "__main__":
    _standalone()
