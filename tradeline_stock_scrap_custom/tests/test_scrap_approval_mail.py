from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestScrapApprovalMail(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.approver = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Scrap approver',
            'login': 'scrap-approver-login',
            'email': 'approval@example.com',
        })
        cls.wizard = cls.env['request.scrap.wizard'].create({'note': 'Approval regression'})

    def _mail_values(self):
        with patch.object(type(self.env['mail.mail']), 'send') as send:
            self.wizard.send_email('https://example.com/scrap/1', self.approver, 'Review')
        send.assert_called_once()
        return self.env['mail.mail'].search([
            ('subject', '=', 'Stock Scrap'), ('email_to', '=', 'approval@example.com'),
        ], order='id desc', limit=1)

    def test_non_email_login_uses_contact_email(self):
        mail = self._mail_values()
        self.assertTrue(mail)
        self.assertEqual(mail.email_to, self.approver.email)

    def test_email_login_does_not_override_contact_email(self):
        self.approver.login = 'old-address@example.com'
        self.assertTrue(self._mail_values())

    def test_missing_or_invalid_email_creates_no_mail(self):
        for email in (False, 'admin', 'one@example.com, two@example.com'):
            with self.subTest(email=email):
                self.approver.email = email
                with patch.object(type(self.env['mail.mail']), 'create') as create:
                    with self.assertRaises(UserError):
                        self.wizard.send_email('https://example.com', self.approver, 'Review')
                create.assert_not_called()

    def test_all_recipients_checked_before_any_send(self):
        invalid = self.approver.copy({
            'login': 'invalid-scrap-approver', 'email': False,
        })
        self.env.ref('tradeline_stock_scrap_custom.group_approval_scrap').users = (
            self.approver | invalid
        )
        with patch.object(type(self.wizard), 'send_email') as send:
            with self.assertRaises(UserError):
                self.wizard.request_scrap()
        send.assert_not_called()
