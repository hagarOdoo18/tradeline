# -*- coding: utf-8 -*-
from odoo import fields, models, api, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'
    
    auto_validate_documents = fields.Boolean(string='Auto Validate Documents',
                                             help='Used to auto validate documents with'
                                                  ' action automatic with Post action.')
    daily_auto_validate_documents = fields.Boolean(string='Daily Auto Validate Documents',
                                                   help='used to auto validate documents daily')
    document_validate_cron_id = fields.Many2one('ir.cron', "Automated Validate Action",
                                                help="This action is responsible for Daily bulk"
                                                     " validate non sent documents")
    auto_pull_received_documents = fields.Boolean(string='Auto Get Vendor Received Documents',
                                                  help='Used to auto Get Vendor Received Documents with'
                                                       ' action automatic action.')
    vendor_received_doc_cron_id = fields.Many2one('ir.cron', "Automated Vendor Received Docs Action",
                                                  help="This action is responsible for Daily bulk"
                                                       " pull vendor received documents")
    eta_description = fields.Selection([('product', 'Product HS Desc.'), ('line', 'Label')],
                                       "ETA Description", default='product', required=False,
                                       help="This action is used to determine if product description from product or "
                                            "line\n- Product HS Desc: this will use value from product HS Description "
                                            "field\n- Label: this will use value from line label field\n")
    eta_expiration_duration = fields.Integer("ETA Expiration Duration", default=7, required=False,
                                             help="This field is used to configure the duration of expiration"
                                                  " which updated from ETA side.")
    
    @api.model
    def default_get(self, fields):
        vals = super(ResConfigSettings, self).default_get(fields)
        vals['document_validate_cron_id'] = self.env.ref('egyptian_electronic_invoice.ir_cron_document_posted_sync').id
        vals['vendor_received_doc_cron_id'] = self.env.ref(
            'egyptian_electronic_invoice.ir_cron_auto_pull_received_documents').id
        return vals
    
    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        res['auto_validate_documents'] = self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.auto_validate_documents')
        # Auto Validate docs
        res['daily_auto_validate_documents'] = self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.daily_auto_validate_documents')
        res['document_validate_cron_id'] = int(self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.document_validate_cron_id'))
        # Auto Get Vendor Docs
        res['auto_pull_received_documents'] = self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.auto_pull_received_documents')
        res['vendor_received_doc_cron_id'] = int(self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.vendor_received_doc_cron_id'))
        # ETA Description Value
        res['eta_description'] = self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.eta_description')
        # ETA Expiration Duration
        res['eta_expiration_duration'] = int(self.env['ir.config_parameter'].sudo().get_param(
            'egyptian_electronic_invoice.eta_expiration_duration')) or 7
        return res
    
    @api.model
    def set_values(self):
        super(ResConfigSettings, self).set_values()
        
        self.env['ir.config_parameter'].sudo().set_param('egyptian_electronic_invoice.auto_validate_documents',
                                                         self.auto_validate_documents)
        # Auto Validate docs
        self.env['ir.config_parameter'].sudo().set_param('egyptian_electronic_invoice.daily_auto_validate_documents',
                                                         self.daily_auto_validate_documents)
        self.env['ir.config_parameter'].sudo().set_param('egyptian_electronic_invoice.document_validate_cron_id',
                                                         int(self.document_validate_cron_id))
        # Auto Get Vendor Docs
        self.env['ir.config_parameter'].sudo().set_param('egyptian_electronic_invoice.auto_pull_received_documents',
                                                         self.auto_pull_received_documents)
        self.env['ir.config_parameter'].sudo().set_param('egyptian_electronic_invoice.vendor_received_doc_cron_id',
                                                         int(self.vendor_received_doc_cron_id))
        # Active/Disable cron job according to setting
        sync_cron_id = self.env.ref('egyptian_electronic_invoice.ir_cron_document_posted_sync')
        get_cron_id = self.env.ref('egyptian_electronic_invoice.ir_cron_auto_pull_received_documents')
        if sync_cron_id.active != self.daily_auto_validate_documents:
            sync_cron_id.write({'active': self.daily_auto_validate_documents})
        if get_cron_id.active != self.auto_pull_received_documents:
            get_cron_id.write({'active': self.auto_pull_received_documents})
        # ETA Description Value
        self.env['ir.config_parameter'].sudo().set_param('egyptian_electronic_invoice.eta_description',
                                                         self.eta_description)
        # ETA Expiration Duration
        self.env['ir.config_parameter'].sudo().set_param('egyptian_electronic_invoice.eta_expiration_duration',
                                                         int(self.eta_expiration_duration))


