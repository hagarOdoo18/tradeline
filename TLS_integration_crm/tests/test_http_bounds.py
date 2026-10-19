"""Transport failures must not become successful TVC invoice deliveries."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from ..models import invoices


class TestTVCHttpBounds(TestCase):
    def test_token_timeout_returns_no_token_without_retry(self):
        with patch.object(invoices.requests, 'post', side_effect=requests.Timeout) as post:
            self.assertFalse(invoices.AccountInvoice._get_token(None))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['timeout'], (5, 30))

    def delivery(self, response=None, error=None):
        audit = SimpleNamespace(state='draft', invoice_number='TEST-ONLY', note=None)
        audit_model = Mock()
        audit_model.search.return_value = False
        audit_model.create.return_value = audit
        invoice = SimpleNamespace(name='TEST-ONLY', move_type='out_invoice',
                                  partner_id=SimpleNamespace(mobile='01000000000'),
                                  is_tvc=False, is_point=False)
        integration = SimpleNamespace(env={'account.invoice.tvc': audit_model},
                                      _get_token=lambda: 'test-token', _cr=Mock())
        with patch.object(invoices.requests, 'post', return_value=response, side_effect=error) as post:
            invoices.AccountInvoice.post_tvc_invoice(integration, {}, invoice, False, 100, 'test')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['timeout'], (5, 30))
        return audit, invoice, integration

    def test_lost_response_is_unconfirmed_without_flags_or_retry(self):
        for error in (requests.Timeout(), requests.ConnectionError()):
            with self.subTest(error=type(error).__name__):
                audit, invoice, integration = self.delivery(error=error)
                self.assertEqual(audit.state, 'error')
                self.assertIn('Verify the invoice at TVC before retrying', audit.note)
                self.assertFalse(invoice.is_tvc)
                self.assertFalse(invoice.is_point)
                integration._cr.commit.assert_not_called()

    def test_confirmed_response_preserves_success_and_commit(self):
        audit, invoice, integration = self.delivery(response=SimpleNamespace(status_code=200, text='accepted'))
        self.assertEqual(audit.state, 'done')
        self.assertTrue(invoice.is_tvc)
        self.assertEqual(audit.note, 'accepted')
        integration._cr.commit.assert_called_once_with()
