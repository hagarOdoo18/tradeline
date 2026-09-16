from odoo import fields, models, api

class ProductFamily(models.Model):
    _name = 'product.family'
    _description = 'Family'


    name = fields.Char(string='Name', required=True)


class SubCategory(models.Model):
    _name = 'sub.category'
    _description = 'Sub Category'


    name = fields.Char(string='Name', required=True)



