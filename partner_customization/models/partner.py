from odoo import fields,models,api
from odoo.exceptions import ValidationError
from odoo.osv import expression

import logging

_logger = logging.getLogger(__name__)


class ResPartnerInherit(models.Model):
    _inherit = 'res.partner'


    mobile_type = fields.Selection(selection=[('local','Local'), ('foreigner', 'Foreigner')],default='local', string='Mobile Type')
    customer = fields.Boolean(
        string='Customer', 
        required=False)
    vendor = fields.Boolean(
        string='Vendor', 
        required=False)

    company_type = fields.Selection(string='Company Type',default='person',
                                    selection=[('person', 'Individual'), ('company', 'Company')],
                                    )
    company_size = fields.Selection(
        string='Employees',
        selection=[
            ('small', '21 small : up to 250'),
            ('medium', '21 medium: 250-1000'),
            ('enterprise', '2L Enterprise : 1000+ local'),
            ('global', '2L Global : 1000+ Foreigner'),
        ],
        required=False,
    )
    company_device = fields.Integer(
        string='Devices',
        required=False)
        
    @api.constrains('mobile')
    def unique_mobile_id(self):
        if self.mobile and self.search([('mobile', '=', self.mobile),
                                        ('id', '!=', self.id)]) and self.env.user.id  not in [2,1]:
            raise ValidationError('Mobile already exists!')


    # _sql_constraints = [
    #     ('vat_uniq', 'unique(vat)', "Vat and National Id  should be unique")
    # ]




    # def write(self, values):
    #     # Add code here
    #     if 'name' in values or 'mobile' in values  :
    #         if self.env.user.id not in [2, 1]:
    #             raise ValidationError('Not Allowed')
    #
    #
    #     return super(ResPartnerInherit, self).write(values)


    def vat_constrain(self):

        if self.vat and self.mobile_type =='local' and self.company_type == 'person' and len(self.vat) != 14:
            raise ValidationError('National Id must be only 14 digits')
        elif  self.vat and self.mobile_type =='local' and self.company_type == 'company' and len(self.vat) != 9:
            raise ValidationError('Vat Number must be only 9 digits')
        elif not self.vat and self.mobile_type =='local' and self.company_type == 'company':
            raise ValidationError('Please Set Vat Number')

    @api.constrains('mobile')
    def mobile_constrain(self):
        if self.mobile:


            existing_mob = self.search([('mobile', 'in', [str(self.mobile)])]) - self
            if len(existing_mob) > 0 and self.env.user.id  not in [2,1] :
                raise ValidationError('mobile number already exist')

            elif self.mobile_type == 'local' and len(self.mobile) in [15,11]:
                raise ValidationError('The local mobile number must be only 11 digits')
        else:
            raise ValidationError('Please Set Mobile Number')

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        args = list(args or [])
        if not name:
            # When no name is provided, call the parent implementation
            return super().name_search(name=name, args=args, operator=operator,
                                       limit=limit)
        # Add search criteria for name, email, and phone
        domain = ['|', '|','|','|',
                  ('name', operator, name),
                  ('email', operator, name),
                  ('mobile', operator, name),
                  ('vat', operator, name),
                  ('phone', operator, name)]
        # Combine with existing args
        if args:
            domain = ['&'] + args + domain
        # Use search_fetch to get both IDs and display_name efficiently
        partners = self.search_fetch(domain, ['display_name'], limit=limit)
        # Return in the expected format: [(id, display_name), ...]
        return [(partner.id, partner.display_name) for partner in partners]
