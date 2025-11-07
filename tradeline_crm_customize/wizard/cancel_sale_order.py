# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _


class CancelSaleOrderPipe(models.TransientModel):
    _name = 'cancel.sale.order.pipeline'



    reason = fields.Text(string="Reason", required=True, )



    
    def action_apply(self):
        self.ensure_one()
        sale_order = self.env['sale.order'].browse(self._context.get('active_ids', []))
        stage = self.env['crm.stage'].search(
            [('probability', '=', 1.00)], limit=1)
        for line in sale_order.order_line:
            if line.product_uom_qty == line.qty_delivered and line.qty_delivered != 0 :
                raise UserError(_("Closed/If any quantity of the order is received, it cannot be returned"))
            else:

                sale_order.action_cancel()
                # sale_order.opportunity_id.type = 'lead'
                sale_order.opportunity_id.stage_id = stage.id
                sale_order.opportunity_id.reason = self.reason
                # edit 22/2/2021
                # add manager
                team = self.env['crm.team'].search(
                    [('member_ids', '=', sale_order.opportunity_id.create_uid.id)], limit=1)
                if team:
                    ids_manager = []
                    for manager in team.manager_ids:
                        title = _("Order Call Center (Admin)")
                        message = _("Check Pipline now Number (Admin) %s") % sale_order.opportunity_id.name
                        self.env['bus.bus'].sendone(
                            (self._cr.dbname, 'res.partner', manager.partner_id.id),
                            {'type': 'user_connection', 'title': title,
                             'message': message, 'partner_id': manager.partner_id.id}
                        )
                        ids_manager.append(manager.partner_id.id)
                    sale_order.message_post(
                        subject=('Order Cames From Call Center (Admin)'),
                        body=('(Admin) Please Check This {}').format(
                            sale_order.opportunity_id.name),
                        # partner_ids=[t.team_id.team_head.partner_id.id],
                        partner_ids=ids_manager,
                    )

                # end add
                sale_order.message_post(
                    subject=('Order Canceled'),
                    body=('Please Check This {}').format(
                        sale_order.opportunity_id.name),
                    # partner_ids=[t.team_id.team_head.partner_id.id],
                    partner_ids=[sale_order.opportunity_id.create_uid.partner_id],
                )
                # print("l.user_id.partner_id.id", sale_order.opportunity_id.partner_id.id)
                title = _("Order Call Center")
                message = _("Check Pipline now Number %s") % sale_order.opportunity_id.name
                self.env['bus.bus'].sendone(
                    (self._cr.dbname, 'res.partner', sale_order.opportunity_id.partner_id.id),
                    {'type': 'user_connection', 'title': title,
                     'message': message, 'partner_id': sale_order.opportunity_id.partner_id.id}
                )
                # sale_order.opportunity_id.type = 'lead'
                sale_order.opportunity_id.stage_canceled = fields.Datetime.now()
                eamil_to_agent_canceled_template_id = sale_order.opportunity_id.company_id.eamil_to_agent_canceled_template_id
                print(" eamil_to_branch_template ", eamil_to_agent_canceled_template_id)
                if eamil_to_agent_canceled_template_id:
                    eamil_to_agent_canceled_template_id.sudo().send_mail(sale_order.opportunity_id.id, force_send=True,
                                                                    notif_layout='mail.mail_notification_light')

        # values = {
        #     'team_id': self.team_id.id,
        # }
        #
        # if self.partner_id:
        #     values['partner_id'] = self.partner_id.id
        #
        # if self.name == 'merge':
        #     leads = self.with_context(active_test=False).opportunity_ids.merge_opportunity()
        #     if not leads.active:
        #         leads.write({'active': True, 'activity_type_id': False, 'lost_reason': False})
        #     if leads.type == "lead":
        #         values.update({'lead_ids': leads.ids, 'user_ids': [self.user_id.id]})
        #         self.with_context(active_ids=leads.ids)._convert_opportunity(values)
        #     elif not self._context.get('no_force_assignation') or not leads.user_id:
        #         values['user_id'] = self.user_id.id
        #         leads.write(values)
        # else:
        #     leads = self.env['crm.lead'].browse(self._context.get('active_ids', []))
        #     values.update({'lead_ids': leads.ids, 'user_ids': [self.user_id.id]})
        #     self._convert_opportunity(values)
        #
        # return leads[0].redirect_opportunity_view()
        #
