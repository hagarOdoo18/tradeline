from odoo import fields, models, api
from random import randint
from odoo.exceptions import UserError

from odoo.http import Controller, request, route

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    reference_number = fields.Char(
        string='Reference Number',
        required=True)
    state = fields.Selection(selection_add=[('to_use', 'To Use'), ('refund', 'Refund')])

    def action_set_to_use(self):
        self.write({
            'state': 'to_use'
        })

    def action_set_to_refund(self):
        self.write({
            'state': 'refund'
        })

    def generate_barcode(self):
        val = 0
        for res in self:
            i = True
            while i:
                barcode = ''.join(["%s" % randint(0, 9) for num in range(0, 13)])
                sale_order = self.search([('barcode', '=', barcode)])
                if len(sale_order) <= 0:
                    # _logger.info("barcode with generate %s", barcode)
                    res.barcode = barcode
                    val = barcode
                    i = False

    barcode = fields.Char(
        string='Barcode',
        required=False)

    @api.model_create_multi
    def create(self, vals_list):
        res = super(SaleOrder, self).create(vals_list)
        if not res.barcode:
            res.generate_barcode()
        return res

    discount_id = fields.Many2one(
        comodel_name='discount.reason',
        string='Discount Reason',
        required=False)

    channel_id = fields.Many2one(
        comodel_name='channel.channel',
        string='Channel',
        required=False)

    courier_id = fields.Many2one(
        comodel_name='courier.courier',
        string='Courier',
        required=False)

    bank_id = fields.Many2one(
        comodel_name='bank.details',
        string='Bank',
        required=False)


    sales_rep_id = fields.Many2one(
        comodel_name='sales.rep',
        string='Sales Rep',
        required=True)

    inv_type = fields.Selection(
        string='Invoice Type',default='invoice',
        selection=[('sro', 'SRO'),('quotation','Quotation'),
                   ('invoice', 'Invoice'), ('debit', 'Debit')],
        required=True, )
    sales_rep_domain = fields.Char(
        string='Sales_rep_domain',
        required=False)
    discount_domain = fields.Char(
        string='Sales_rep_domain',
        required=False)

    product_notes = fields.Char(
        string='Product Notes',
        required=False)

    @api.onchange('order_line')
    def onchange_order_line_product_note(self):
        for line in self.order_line:
            if line.product_id.product_notes != '':
                self.product_notes == line.product_id.product_notes
    def action_draft(self):
        return self.write({
            'state': 'draft',
            'signature': False,
            'signed_by': False,
            'signed_on': False,
        })

    @api.onchange('branch_id')
    def onchange_branch_id(self):
        if self.branch_id:
            self.sales_rep_domain = "['|',('branch_id','=',"+str(self.branch_id.id)+"),('branch_id','=',False)]"
            self.discount_domain = "[('branches_ids','in',"+str(self.branch_id.id)+"),('state','=','run')]"

        else:
            self.sales_rep_domain="[('branch_id','=',0)]"
            self.discount_domain="[('branches_ids','=',0)]"

    def _prepare_invoice(self):
        res = super(SaleOrder, self)._prepare_invoice()
        journal = self.env['account.journal'].search([('type','=','sale'),('branch_id','=',self.branch_id.id),('currency_id','=',self.currency_id.id)])
        if not journal:
            raise UserError(('please set Journal for this Branch'))
        res['journal_id'] = journal.id
        res['opportunity_id'] = self.opportunity_id.id
        res['discount_id'] = self.discount_id.id
        res['courier_id'] = self.courier_id.id
        res['channel_id'] = self.channel_id.id
        res['product_notes'] = self.product_notes
        res['bank_id'] = self.bank_id.id
        res['sales_rep_id'] = self.sales_rep_id.id
        res['inv_type'] = self.inv_type
        res['reference_number'] = self.reference_number
        res['barcode'] = self.barcode
        res['pricelist_id'] = self.pricelist_id.id

        res['invoice_date'] = fields.Date.today()
        return res

    def action_confirm(self):
        """ Override of `sale` to send the order to Gelato on confirmation. """
        res = super(SaleOrder, self).action_confirm()
        for rec in self:
            if rec.discount_id:
                for line in rec.order_line:
                    if line.discount <= 0:
                        raise UserError("Remove Discount Reason")

        return res

    tax_t1 = fields.Float(compute='_compute_tax', string="VAT14%")
    tax_t2 = fields.Float(compute='_compute_tax', string="VAT1%")
    tax_t2_t = fields.Float(compute='_compute_tax', string="VAT2%")
    tax_t3 = fields.Float(compute='_compute_tax', string="VAT3%")
    tax_t5 = fields.Float(compute='_compute_tax', string="VAT5%")
    total = fields.Float(compute='compute_tax', string="Total")

    @api.depends('order_line')
    def _compute_tax(self):
        for rec in self:
            sum_v14 = 0
            sum_v1 = 0
            sum_v3 = 0
            sum_v5 = 0
            sum_v2 = 0

            for line in rec.order_line:
                if line.tax_ids:
                    for tax in line.tax_ids:
                        if tax.name == "14%":
                            sum_v14 += (line.price_subtotal * tax.amount / 100)

                        elif tax.name == "Withholding Tax -1%":
                            sum_v1 += (line.price_subtotal * tax.amount / 100)

                        elif tax.name == "Withholding Tax -3%":
                            sum_v3 += (line.price_subtotal * tax.amount / 100)
                        elif tax.name == "Withholding Tax -5%":
                            sum_v5 += (line.price_subtotal * tax.amount / 100)
                        elif tax.name == "Withholding Tax -2%":
                            sum_v2 += (line.price_subtotal * tax.amount / 100)

            rec.tax_t1 = sum_v14
            rec.tax_t2 = sum_v1
            rec.tax_t3 = sum_v3
            rec.tax_t5 = sum_v5
            rec.tax_t2_t = sum_v2
            rec.total = sum_v14 + rec.amount_untaxed

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    product_point = fields.Float(
        string='Product point',
        required=False)
    product_incentive = fields.Float(
        string='Product incentive',
        required=False)
    warranty_id = fields.Many2one(
        comodel_name='product.warranty',
        string='Warranty',
        required=False)

    item_code = fields.Char(
        string='Item Code',
        required=False)

    categ_id = fields.Many2one(
        comodel_name='product.category',
        string='Category',
        required=False)

    family_id = fields.Many2one(
        comodel_name='product.family',
        string='Family',
        required=False)

    location_id = fields.Many2one(
        'stock.location',
        string="Stock Location",
        compute='_compute_location_id',
        store=True
    )



    @api.depends('order_id.warehouse_id')
    def _compute_location_id(self):
        for line in self:
            line.location_id = line.order_id.warehouse_id.lot_stock_id


    @api.onchange('product_id')
    def _onchange_product_id_set_values(self):
        self.item_code = self.product_id.default_code
        self.family_id = self.product_id.product_tmpl_id.family_id.id
        self.categ_id = self.product_id.categ_id.id
        warranty =  self.env['product.warranty'].search([('categ_ids','in',self.product_id.categ_id.id)])
        if warranty:
            self.warranty_id =  warranty.id


    def _prepare_invoice_line(self, **optional_values):

        res = super()._prepare_invoice_line(**optional_values)
        warranty = self.env['product.warranty'].search([('categ_ids', 'in', self.product_id.categ_id.id)])

        res['warranty_id'] = warranty.id if warranty else False
        res['item_code'] =  self.product_id.default_code
        res['family_id'] = self.product_id.product_tmpl_id.family_id.id
        res['categ_id'] = self.product_id.categ_id.id
        return res

    @api.onchange('discount')
    def _onchange_discount(self):
        for line in self:
            if line.product_id:
                if line.discount != 0 and not line.order_id.discount_id:
                    raise UserError("Select Discount Reason To Apply Discount")
                elif line.discount > line.order_id.discount_id.discount_percentage:
                    raise UserError("Discount Not Matched with Discount Reason")

    @api.onchange('product_id')
    def _onchange_product_id (self):
        if not self.order_id.partner_id:
            raise UserError("Select Customer First")

    lot_id = fields.Many2one(
        "stock.lot",
        "Lot",
        copy=False,
        readonly=False,
    )

    @api.onchange("product_id","qty_delivered")
    def _onchange_lot_id(self):
        for sol in self:

            sol.product_point = sol.product_id.product_tmpl_id.product_point * sol.qty_delivered

    @api.onchange("lot_id")
    def _onchange_lot_id(self):
        for sol in self:
            if sol.lot_id.product_id:
                sol.product_id = sol.lot_id.product_id.id

class saleadvancepaymentinv(models.TransientModel):
    _inherit = 'sale.advance.payment.inv'
    _name = 'sale.advance.payment.inv'

    advance_payment_method = fields.Selection(
        selection=[
            ('delivered', "Regular invoice"),
            ('percentage', "Down payment (percentage)"),
            ('fixed', "Down payment (fixed amount)"),
        ],
        string="Create Invoice",
        default='delivered',
        required=True,
        readonly=True,
        help="A standard invoice is issued with all the order lines ready for invoicing,"
             "according to their invoicing policy (based on ordered or delivered quantity).")

    def _create_invoices(self,sale_orders):
        invoices = super(saleadvancepaymentinv, self)._create_invoices(sale_orders)
        for invoice in invoices:
            invoice.action_post()
        return invoices

# class SaleProductConfiguratorController(Controller):
#
#     def _get_product_information(
#         self,
#         product_template,
#         combination,
#         currency,
#         pricelist,
#         so_date,
#         quantity=1,
#         product_uom_id=None,
#         parent_combination=None,
#         **kwargs,
#     ):
#         """ Return complete information about a product.
#
#         :param product.template product_template: The product for which to seek information.
#         :param product.template.attribute.value combination: The combination of the product.
#         :param res.currency currency: The currency of the transaction.
#         :param product.pricelist pricelist: The pricelist to use.
#         :param datetime so_date: The date of the `sale.order`, to compute the price at the right
#             rate.
#         :param int quantity: The quantity of the product.
#         :param int|None product_uom_id: The unit of measure of the product, as a `uom.uom` id.
#         :param product.template.attribute.value|None parent_combination: The combination of the
#             parent product.
#         :param dict kwargs: Locally unused data passed to `_get_basic_product_information`.
#         :rtype: dict
#         :return: A dict with the following structure:
#             {
#                 'product_tmpl_id': int,
#                 'id': int,
#                 'description_sale': str|False,
#                 'display_name': str,
#                 'price': float,
#                 'quantity': int
#                 'attribute_line': [{
#                     'id': int
#                     'attribute': {
#                         'id': int
#                         'name': str
#                         'display_type': str
#                     },
#                     'attribute_value': [{
#                         'id': int,
#                         'name': str,
#                         'price_extra': float,
#                         'html_color': str|False,
#                         'image': str|False,
#                         'is_custom': bool
#                     }],
#                     'selected_attribute_id': int,
#                 }],
#                 'exclusions': dict,
#                 'archived_combination': dict,
#                 'parent_exclusions': dict,
#             }
#         """
#         product_uom = request.env['uom.uom'].browse(product_uom_id)
#         product = product_template._get_variant_for_combination(combination)
#         attribute_exclusions = product_template._get_attribute_exclusions(
#             parent_combination=parent_combination,
#             combination_ids=combination.ids,
#         )
#         product_or_template = product or product_template
#
#         values = dict(
#             product_tmpl_id=product_template.id,
#             **self._get_basic_product_information(
#                 product_or_template,
#                 pricelist,
#                 combination,
#                 quantity=quantity,
#                 uom=product_uom,
#                 currency=currency,
#                 date=so_date,
#                 **kwargs,
#             ),
#             quantity=quantity,
#             attribute_lines=[dict(
#                 id=ptal.id,
#                 attribute=dict(**ptal.attribute_id.read(['id', 'name', 'display_type'])[0]),
#                 attribute_values=[
#                     dict(
#                         **ptav.read(['name', 'html_color', 'image', 'is_custom'])[0],
#                         price_extra=self._get_ptav_price_extra(
#                             ptav, currency, so_date, product_or_template
#                         ),
#                     ) for ptav in ptal.product_template_value_ids
#                     if ptav.ptav_active or combination and ptav.id in combination.ids
#                 ],
#                 selected_attribute_value_ids=combination.filtered(
#                     lambda c: ptal in c.attribute_line_id
#                 ).ids,
#                 create_variant=ptal.attribute_id.create_variant,
#             ) for ptal in product_template.attribute_line_ids],
#             exclusions=attribute_exclusions['exclusions'],
#             archived_combinations=attribute_exclusions['archived_combinations'],
#             parent_exclusions=attribute_exclusions['parent_exclusions'],
#         )
#         # Shouldn't be sent client-side
#         values.pop('pricelist_rule_id', None)
#         return values