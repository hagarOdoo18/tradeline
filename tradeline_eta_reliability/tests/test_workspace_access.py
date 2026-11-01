from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestEtaWorkspaceAccess(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.central = new_test_user(cls.env, login='eta_central_workspace_qa',
            groups='account.group_account_invoice,tradeline_eta_reliability.group_eta_workspace')
        cls.branch = new_test_user(cls.env, login='eta_branch_workspace_qa',
            groups='account.group_account_invoice,base.group_system')

    def test_branch_cannot_open_workspace_actions_even_when_metadata_is_sudoed(self):
        action = self.env.ref('tradeline_eta_reliability.action_eta_all_invoices')
        for sudo in (False, True):
            caller = action.with_user(self.branch)
            caller = caller.sudo() if sudo else caller
            with self.assertRaises(AccessError):
                caller._get_action_dict()
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.branch).action_eta_today()
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.branch).action_eta_find_invoice('any invoice')

    def test_branch_workspace_menus_remain_hidden_after_accidental_group_reset(self):
        root = self.env.ref('tradeline_eta_reliability.menu_account_eta_jobs')
        child = self.env.ref('tradeline_eta_reliability.menu_eta_all_invoices')
        (root | child).groups_id = self.env.ref('account.group_account_invoice')
        for debug in (False, True):
            visible = self.env['ir.ui.menu'].with_user(self.branch)._visible_menu_ids(debug=debug)
            self.assertNotIn(root.id, visible)
            self.assertNotIn(child.id, visible)

    def test_ordinary_central_accountant_can_open_workspace_without_manager_rights(self):
        self.assertFalse(self.central.has_group('account.group_account_manager'))
        self.assertFalse(self.central.has_group('base.group_system'))
        action = self.env.ref('tradeline_eta_reliability.action_eta_all_invoices').with_user(self.central)._get_action_dict()
        self.assertEqual(action['res_model'], 'account.move')
        self.env['tradeline.eta.job'].with_user(self.central).get_view(view_type='list')
        self.env['account.move'].with_user(self.central).action_eta_today()

    def test_branch_keeps_standard_invoice_form_without_central_controls(self):
        move = self.env['account.move'].with_user(self.branch)
        view = etree.fromstring(move.get_view(view_type='form')['arch'])
        self.assertTrue(view.xpath("//field[@name='eta_staff_state']"))
        self.assertFalse(view.xpath("//button[@name='action_eta_status_details']"))
        self.assertFalse(view.xpath("//button[@name='action_eta_correct_vat']"))
        self.assertFalse(view.xpath("//widget[@name='eta_connection_health']"))
        with self.assertRaises(AccessError):
            move.get_view(view_id=self.env.ref('tradeline_eta_reliability.eta_workspace_invoice_list').id, view_type='list')
        for kind in ('list', 'search'):
            with self.assertRaises(AccessError):
                move.with_context(**{kind + '_view_ref': 'tradeline_eta_reliability.eta_workspace_invoice_' + kind}).get_view(view_type=kind)
        with self.assertRaises(AccessError):
            self.env['tradeline.eta.job'].with_user(self.branch).get_view(view_type='list')

    def test_workspace_permission_does_not_grant_financial_model_access(self):
        group = self.env.ref('tradeline_eta_reliability.group_eta_workspace')
        self.assertFalse(group.implied_ids)
        user = new_test_user(self.env, login='eta_workspace_without_invoice_qa',
            groups='base.group_user,tradeline_eta_reliability.group_eta_workspace')
        self.assertFalse(self.env['account.move'].with_user(user).has_access('write'))
        self.assertFalse(self.env['account.move.line'].with_user(user).has_access('write'))
