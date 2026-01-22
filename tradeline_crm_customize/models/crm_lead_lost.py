# -*- coding: utf-8 -*-

from odoo import api, fields, models,_


class CrmLeadLost(models.TransientModel):
    _inherit = 'crm.lead.lost'
    _name = 'crm.lead.lost'


    def action_lost_reason_apply(self):
        leads = self.env['crm.lead'].browse(self.env.context.get('active_ids'))

        stage = self.env['crm.stage'].search(
            [('name', '=','Lost')],limit=1)
        for l in leads:
            l.stage_id = stage.id
            # edit 22/2/2021
            # add manager
            team = self.env['crm.team'].search(
                [('member_ids', '=', l.create_uid.id)], limit=1)
            if team:
                ids_manager = []
                for manager in team.manager_ids:
                    title = _("Order Call Center (Admin)")
                    message = _("Check Pipline now Number (Admin) %s") % l.name

                    ids_manager.append(manager.partner_id.id)
                l.message_post(
                    subject=('Order Cames From Call Center (Admin)'),
                    body=('(Admin) Please Check This {}').format(
                        l.name),
                    # partner_ids=[t.team_id.team_head.partner_id.id],
                    partner_ids=ids_manager,
                )

            # end add
            l.message_post(
                            subject=('Order Cames From Call Center'),
                            body=('Please Check This {}').format(
                                l.name),
                            # partner_ids=[t.team_id.team_head.partner_id.id],
                            partner_ids=[l.create_uid.partner_id],
                        )
            title = _("Order Call Center")
            message = _("Check Pipline now Number %s") % l.name

            # leads.type = 'lead'
            leads.stage_lost = fields.Datetime.now()
            eamil_to_agent_lost_template_id = leads.company_id.eamil_to_agent_lost_template_id
            if eamil_to_agent_lost_template_id:
                eamil_to_agent_lost_template_id.sudo().send_mail(l.id, force_send=True,
                                                          notif_layout='mail.mail_notification_light')
        res = self.lead_ids.action_set_lost(lost_reason_id=self.lost_reason_id.id)
        return res
