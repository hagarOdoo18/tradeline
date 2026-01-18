from odoo import fields, models, api


class ProductTemplate(models.Model):
    _inherit = 'product.product'

    e_invoicing_code = fields.Char(string='E-invoicing Product code', readonly=True)
    code_type = fields.Selection(string="Code Type", selection=[('gs1', 'GS1'), ('egs', 'EGS')], default='egs')
    gs1_code = fields.Char(string="GS1 Code", required=False)
    _sql_constraints = [
        ('e_invoicing_code', 'unique(e_invoicing_code)', 'Unique e_invoicing_code.')
    ]
    ar_description = fields.Char(
        string='Ar Description',
        required=False)

    @api.model_create_multi
    def create(self, vals):
        for val in vals:
            code = self.env['ir.sequence'].next_by_code(
                'product.product')
            tax_id = self.env.user.company_id.vat if self.env.user.company_id.vat else self.env.user.company_id.anther_vat
            new_code = "EG-" + tax_id + "-" + code
            val['e_invoicing_code'] = new_code
        return super().create(vals)

    @api.model
    def set_old_product(self):
        all_products = self.search([('e_invoicing_code','=','')])
        for product in all_products:
            code = self.env['ir.sequence'].next_by_code(
                'product.product')
            tax_id = self.env.user.company_id.vat if self.env.user.company_id.vat else self.env.user.company_id.anther_vat
            new_code = "EG-" + tax_id + "-" + code
            product.e_invoicing_code =new_code


class ResCompany(models.Model):
    _inherit = 'res.company'

    anther_vat = fields.Char(
        string='Anther Vat',
        required=False)
