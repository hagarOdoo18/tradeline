from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestBoundedPreview(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param("legacy_invoice_archive.report_preview_row_limit", "2")
        self.invoices = self.env["legacy.invoice"].create([
            {"source_db": "preview_test", "source_id": i, "number": str(i), "invoice_date": "2099-01-01"}
            for i in range(1, 5)
        ])

    def _wizard(self, code):
        pack = self.env["legacy.report.pack.definition"].create({"name": "Preview test", "code": code})
        return self.env["legacy.report.pack.generate.wizard"].create({
            "report_pack_id": pack.id, "date_from": "2099-01-01", "date_to": "2099-01-01",
        })

    def test_invoice_preview_limits_before_building_rows(self):
        wizard = self._wizard("invoice_standard")
        cls = type(wizard.report_pack_id)
        original = cls._build_report_rows
        sizes = []

        def build(pack, invoices, **kwargs):
            sizes.append(len(invoices))
            return original(pack, invoices, **kwargs)

        with patch.object(cls, "_build_report_rows", build):
            payload = wizard.get_preview_payload()
        self.assertEqual(sizes, [2])
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["total_rows"], 4)
        self.assertEqual(payload["invoice_count"], 4)
        self.assertTrue(payload["truncated"])
        # Full exports retain all matching rows.
        _, rows = wizard.report_pack_id._build_report_rows(wizard.report_pack_id._get_invoices(wizard))
        self.assertEqual(len(rows), 4)

    def test_serial_preview_limits_child_rows(self):
        wizard = self._wizard("delivery")
        self.env["legacy.invoice.serial.ref"].create([
            {"invoice_id": invoice.id, "lot_name": f"SERIAL-{invoice.id}-{i}"}
            for invoice in self.invoices for i in range(3)
        ])
        payload = wizard.get_preview_payload()
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["total_rows"], 12)
        self.assertEqual(payload["invoice_count"], 4)

    def test_invalid_or_negative_limit_still_bounds_preview(self):
        wizard = self._wizard("invoice_standard")
        self.env["ir.config_parameter"].sudo().set_param("legacy_invoice_archive.report_preview_row_limit", "-1")
        payload = wizard.get_preview_payload()
        self.assertEqual(payload["row_limit"], 1)
        self.assertEqual(len(payload["rows"]), 1)

    def test_payment_preview_limits_child_rows_and_keeps_full_export(self):
        wizard = self._wizard("payment_receipt")
        self.env["legacy.invoice.payment.link"].create([
            {"invoice_id": invoice.id, "source_payment_id": i, "amount": 10, "name": f"PAYMENT-{i}"}
            for invoice in self.invoices for i in range(1, 4)
        ])
        payload = wizard.get_preview_payload()
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["total_rows"], 12)
        _, rows = wizard.report_pack_id._build_report_rows(wizard.report_pack_id._get_invoices(wizard))
        self.assertEqual(len(rows), 12)
