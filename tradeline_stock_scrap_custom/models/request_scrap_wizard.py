from odoo import models, fields, _
from odoo.exceptions import UserError
from odoo.tools import email_normalize


class RequestScrapWizard(models.TransientModel):
    _name = 'request.scrap.wizard'
    _description = 'Request Scrap Wizard'

    note = fields.Text(string="Note", required=True)

    def request_scrap(self):
        self.ensure_one()
        # Existing databases can have several approval groups with this name.
        # Preserve the original recipient set across all matching groups.
        approve_group = self.env['res.groups'].search([('name', '=', 'Approve Scrap')])
        approvers = approve_group.users.filtered('active')
        # Check every recipient before sending anything or changing approval state.
        for user in approvers:
            self._approval_email(user)
        if self.env.context.get('default_picking_id'):
            picking = self.env['stock.picking'].browse(self.env.context.get('active_id'))
            link = '%s/odoo/action-stock.action_picking_tree_all/%s' % (
                'http://tradelineodoo.com', picking.id)
            for user in approvers:
                self.send_email(link, user, self.note)
                picking.message_post(body=_("Send Mail For Approve Scrap [ %s ]", user.name))
                picking.request_scrap = True
                picking.wait_approve_scrap = True
        else:
            scrap = self.env['stock.scrap'].browse(self.env.context.get('active_id'))
            scrap.state = 'witting'
            link = '%s/odoo/action-stock.action_stock_scrap/%s' % (
                'http://tradelineodoo.com', scrap.id)
            for user in approvers:
                self.send_email(link, user, self.note)
                scrap.message_post(body=_("Send Mail For Approve Scrap [ %s ]", user.name))

    def _approval_email(self, user):
        email = email_normalize(user.email)
        if not email:
            raise UserError(_(
                "Set a valid email address for scrap approver %s before requesting approval.",
                user.display_name,
            ))
        return email

    def send_email(self, link, approval_list, note):
        email = self._approval_email(approval_list)
        mail_content = (
            "<p>Please Approve Scrap</p>"
            "<a href=%s>Stock Scrap</a><br/> [ %s ]" % (link, note)
        )
        main_content = {
            'subject': "Stock Scrap",
            'author_id': self.env.user.partner_id.id,
            'body_html': mail_content,
            'email_to': email,
            'email_from': 'tlsodoo18@gmail.com',
        }
        mail = self.env['mail.mail'].create(main_content)
        mail.send(True)
