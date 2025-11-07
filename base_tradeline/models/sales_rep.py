from odoo import fields, models, api


class SalesRep(models.Model):
    _name = 'sales.rep'
    _description = 'Sales Rep'

    name = fields.Char()

    mail = fields.Char(
        string='E-Mail',
        required=False)

    type = fields.Selection(
        string='Type',
        selection=[('stock', 'Stock'),
                   ('sales', 'Sales'), ],
        required=False, )

    code = fields.Integer(
        string='Code', 
        required=False)

    is_call_center = fields.Boolean(
        string='call center',
        required=False)

    def name_get(self):
        # TDE: this could be cleaned a bit I think
        result=[]
        def _name_get(d):
            name = d.get('name', '')
            code = d.get('code', False) or False
            if code:
                name = '[%s] %s' % (code, name)
            return (d['id'], name)

            for sales_rep in self.sudo():
                mydict = {
                    'id': sales_rep.id,
                    'name': sales_rep.anme,
                    'code': sales_rep.code,
                }
            result.append(_name_get(mydict))
        return result




    @api.model
    def _load_pos_data_domain(self, data):
        return []

    @api.model
    def _load_pos_data_fields(self, config_id):
        return ['id', 'name']

    def _load_pos_data(self, data):
        domain = self._load_pos_data_domain(data)
        fields = self._load_pos_data_fields(data['pos.config']['data'][0]['id'])
        delivery_providers = self.search_read(domain, fields, load=False)
        return {
            'data': delivery_providers,
            'fields': fields,
        }
