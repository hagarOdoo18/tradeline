# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _


class Lead2OpportunityPartner_inher(models.TransientModel):
    _inherit = 'crm.lead2opportunity.partner'
    _name = 'crm.lead2opportunity.partner'


    team_id = fields.Many2one('crm.team', 'Sales Team', oldname='section_id', index=True,readonly=True)
    user_id = fields.Many2one('res.users', 'Salesperson', index=True)

    @api.model
    def default_get(self, fields):
        """ Default get for name, opportunity_ids.
            If there is an exisitng partner link to the lead, find all existing
            opportunities links with this partner to merge all information together
        """
        result = super(Lead2OpportunityPartner_inher, self).default_get(fields)
        active_model = self.env.context.get('active_model')
        if active_model == 'crm.lead':
            lead = self.env[active_model].browse(self.env.context.get('active_id')).exists()
            if lead:

                result['branch_id'] = lead.branch_id.id
                result['team_id'] = lead.team_id.id
                result['user_id'] = lead.branch_id.user_id.id

        return result

    @api.depends('user_id')
    def _compute_team_id(self):
        """ When changing the user, also set a team_id or restrict team id
        to the ones user_id is member of. """
        for convert in self:
            # setting user as void should not trigger a new team computation
            if not convert.user_id:
                continue
            user = convert.user_id
            if convert.team_id and user in convert.team_id.member_ids | convert.team_id.user_id:
                continue
            if not convert.team_id:
                team = self.env['crm.team']._get_default_team_id(user_id=user.id, domain=None)
                convert.team_id = team.id

    def action_apply(self):
        res = super(Lead2OpportunityPartner_inher, self).action_apply()

        leads = self.env['crm.lead'].browse(self._context.get('active_ids', []))

        for l in leads:
            leads.confirm_data = fields.Datetime.now()
            leads.stage_pipeline = fields.Datetime.now()
            # edit 22/2/2021
            # add manager
            team = self.env['crm.team'].search(
                [('member_ids', '=', l.create_uid.id)], limit=1)
            if team:
                ids_manager = []
                for manager in team.member_ids:
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
                                self.name),
                            # partner_ids=[t.team_id.team_head.partner_id.id],
                            partner_ids=[l.user_id.partner_id.id],
                        )
            title = _("Order Call Center")
            message = _("Check Pipline now Number %s") % l.name

            eamil_to_branch_template = l.company_id.eamil_to_branch_template_id
            if eamil_to_branch_template:
                eamil_to_branch_template.sudo().send_mail(l.id, force_send=True,
                                                    notif_layout='mail.mail_notification_light')
        return res



