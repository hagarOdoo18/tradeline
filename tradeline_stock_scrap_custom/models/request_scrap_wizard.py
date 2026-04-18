from odoo import models, fields, _


class RequestScrapWizard(models.TransientModel):
    _name = 'request.scrap.wizard'
    _description = 'Request Scrap Wizard'

    note = fields.Text(string="Note", required=True)

    def request_scrap(self):
        if self.env.context.get('default_picking_id'):
            picking = self.env['stock.picking'].browse(self.env.context.get('active_id'))
            link = '%s/odoo/action-stock.action_picking_tree_all/%s' % (
                'http://tradelineodoo.com', picking.id)
            approve_group = self.env['res.groups'].search([('name', '=', 'Approve Scrap')])
            for user in approve_group.users:
                self.send_email(link, user, self.note)
                picking.message_post(body=_("Send Mail For Approve Scrap [ %s ]", user.name))
                picking.request_scrap = True
                picking.wait_approve_scrap = True
        else:
            scrap = self.env['stock.scrap'].browse(self.env.context.get('active_id'))
            scrap.state = 'witting'
            link = '%s/odoo/action-stock.action_stock_scrap/%s' % (
                'http://tradelineodoo.com', scrap.id)
            approve_group = self.env['res.groups'].search([('name', '=', 'Approve Scrap')])
            for user in approve_group.users:
                self.send_email(link, user, self.note)
                scrap.message_post(body=_("Send Mail For Approve Scrap [ %s ]", user.name))

    def send_email(self, link, approval_list, note):
        mail_content = (
            "<p>Please Approve Scrap</p>"
            "<a href=%s>Stock Scrap</a><br/> [ %s ]" % (link, note)
        )
        main_content = {
            'subject': "Stock Scrap",
            'author_id': self.env.user.partner_id.id,
            'body_html': mail_content,
            'email_to': approval_list.login,
            'email_from': 'tlsodoo18@gmail.com',
        }
        mail = self.env['mail.mail'].create(main_content)
        mail.send(True)
