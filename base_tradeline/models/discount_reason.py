from odoo import _, fields, models, api
from odoo.exceptions import ValidationError


class DiscountReason (models.Model):
    _name = 'discount.reason'
    _description = 'Discount Reason'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(required=True)
    start_date = fields.Date(
        string='Start Date',tracking=True,
        required=True)
    end_date = fields.Date(
        string='End Date',tracking=True,
        required=True)

    company_ids= fields.Many2many(
        comodel_name='res.company',
        string='Companies',tracking=True,
        required=True)

    discount_percentage = fields.Float(
        string='Discount Percentage (%)',tracking=True,
        required=True)
    discount_type = fields.Selection(
        selection=[
            ('percentage', 'Percentage'),
            ('fixed_amount', 'Fixed Amount'),
        ],
        string='Discount Type',
        default='percentage',
        required=True,
        tracking=True,
    )
    fixed_discount_amount = fields.Float(
        string='Fixed Discount Amount',
        tracking=True,
        default=0.0,
    )

    state = fields.Selection(
        string='State',
        selection=[('new', 'New'),
                   ('run', 'Running'),('stopped','Stopped') ],default='new', tracking=True,
        required=False, )


    def action_run(self):
        self.state = 'run'


    def action_stop(self):
        self.state = 'stopped'

    def action_new(self):
        self.state = 'new'


    @api.model
    def _load_pos_data_domain(self, data):
        return []

    @api.model
    def _load_pos_data_fields(self, config_id):
        return ['id', 'name', 'discount_percentage', 'discount_type', 'fixed_discount_amount']

    def _load_pos_data(self, data):
        domain = self._load_pos_data_domain(data)
        fields = self._load_pos_data_fields(data['pos.config']['data'][0]['id'])
        delivery_providers = self.search_read(domain, fields, load=False)
        return {
            'data': delivery_providers,
            'fields': fields,
        }

    @api.constrains('discount_type', 'discount_percentage', 'fixed_discount_amount')
    def _check_discount_reason_amount_configuration(self):
        for reason in self:
            if reason.discount_type == 'percentage':
                if reason.discount_percentage < 0 or reason.discount_percentage > 100:
                    raise ValidationError(_("Discount percentage must be between 0 and 100."))
                continue

            if reason.fixed_discount_amount < 0:
                raise ValidationError(_("Fixed discount amount cannot be negative."))
