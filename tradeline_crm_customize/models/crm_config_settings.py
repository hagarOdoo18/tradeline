# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'


    eamil_to_branch_template_id = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template (New Order)')
    eamil_to_agent_won_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Won)')
    eamil_to_agent_lost_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Lost)')
    eamil_to_agent_canceled_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template ( Cancelled )')
    eamil_to_branch_canceled_template_id = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template ( Cancelled )')
    eamil_to_agent_refunded_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Refunded)')
    # admin
    eamil_to_branch_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template (New Order)')
    eamil_to_agent_won_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Won)')
    eamil_to_agent_lost_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Lost)')
    eamil_to_agent_canceled_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template ( Cancelled )')
    eamil_to_branch_canceled_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template ( Cancelled )')
    eamil_to_agent_refunded_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Refunded)')
class HelpdeskSettings(models.TransientModel):
    _inherit = 'res.config.settings'


    eamil_to_branch_template_id = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template (New Order)', related='company_id.eamil_to_branch_template_id', readonly=False)
    eamil_to_agent_won_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Won)', related='company_id.eamil_to_agent_won_template_id', readonly=False)
    eamil_to_agent_lost_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Lost)', related='company_id.eamil_to_agent_lost_template_id', readonly=False)
    eamil_to_agent_canceled_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template ( Cancelled )', related='company_id.eamil_to_agent_canceled_template_id', readonly=False)
    eamil_to_branch_canceled_template_id = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template ( Cancelled )',
        related='company_id.eamil_to_branch_canceled_template_id', readonly=False)
    eamil_to_agent_refunded_template_id = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Refunded)', related='company_id.eamil_to_agent_refunded_template_id', readonly=False)

    # admin

    eamil_to_branch_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template (New Order)', related='company_id.eamil_to_branch_template_id_admin', readonly=False)
    eamil_to_agent_won_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Won)', related='company_id.eamil_to_agent_won_template_id_admin', readonly=False)
    eamil_to_agent_lost_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Lost)', related='company_id.eamil_to_agent_lost_template_id_admin', readonly=False)
    eamil_to_agent_canceled_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template ( Cancelled )', related='company_id.eamil_to_agent_canceled_template_id_admin', readonly=False)
    eamil_to_branch_canceled_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Branch To User Mail Template ( Cancelled )', related='company_id.eamil_to_branch_canceled_template_id_admin', readonly=False)
    eamil_to_agent_refunded_template_id_admin = fields.Many2one(
        'mail.template', string='Email To Agent To User Mail Template (Refunded)', related='company_id.eamil_to_agent_refunded_template_id_admin', readonly=False)
