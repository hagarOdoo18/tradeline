from odoo import fields, models, api


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

    def set_draft(self):

        self.state='draft'
    @api.model
    def _load_pos_data_domain(self, data):
        return []

    @api.model
    def _load_pos_data_fields(self, config_id):
        return ['id', 'name', 'discount_percentage']

    def _load_pos_data(self, data):
        domain = self._load_pos_data_domain(data)
        fields = self._load_pos_data_fields(data['pos.config']['data'][0]['id'])
        delivery_providers = self.search_read(domain, fields, load=False)
        return {
            'data': delivery_providers,
            'fields': fields,
        }
