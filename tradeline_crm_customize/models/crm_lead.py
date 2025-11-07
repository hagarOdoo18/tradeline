
from datetime import datetime, timedelta
from functools import partial
from itertools import groupby

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import formatLang
from odoo.osv import expression
from odoo.tools import float_is_zero, float_compare


from odoo.addons import decimal_precision as dp

from werkzeug.urls import url_encode



class CrmLead(models.Model):
    _inherit = 'crm.lead'


    type = fields.Selection([('lead', 'Lead'), ('opportunity', 'Opportunity')], index=True, required=True,
        default='lead',copy=True,
        help="Type is used to separate Leads and Opportunities")
    name = fields.Char('Opportunity', required=False,readonly=True,store=True, index=True , copy=False)
    confirm_data = fields.Datetime(string="Confirm Data", required=False, )


    def _convert_opportunity_data(self, customer, team_id=False):
        """ Extract the data from a lead to create the opportunity
            :param customer : res.partner record
            :param team_id : identifier of the Sales Team to determine the stage
        """
        if not team_id:
            team_id = self.team_id.id if self.team_id else False
        value = {
            'probability': self.probability,
            'name': self.name,
            'partner_id': customer.id if customer else False,
            'type': 'opportunity',
            'date_open': fields.Datetime.now(),
            'email_from': customer.email if  customer  else  self.email_from,
            'phone': customer.phone if  customer  else self.phone,
            'date_conversion':  self.env.cr.now(),
        }
        if not self.stage_id:
            stage = self._stage_find(team_id=team_id)
            value['stage_id'] = stage.id
            if stage:
                value['probability'] = stage.probability
        return value

    def convert_opportunity(self, partner_id, user_ids=False, team_id=False):

        customer = False
        if partner_id:
            customer = partner_id
        for lead in self:
            if not lead.active or lead.probability == 100:
                continue
            vals = lead._convert_opportunity_data(customer, team_id)
            lead.write(vals)

        if user_ids or team_id:
            self._handle_salesmen_assignment(user_ids=user_ids, team_id=team_id)

        return True

    def _onchange_partner_id(self):
        values = self._onchange_partner_id_values(self.partner_id.id if self.partner_id else False)
        self.update(values)


    order_line = fields.One2many(comodel_name="crm.lead.line",
                                 inverse_name="order_id",copy=False)

    amount_untaxed = fields.Float(string='Untaxed Amount', store=True, readonly=True, compute='_amount_all', track_visibility='onchange', track_sequence=5)

    amount_tax = fields.Float(string='Taxes', store=True, readonly=True, compute='_amount_all')

    amount_total = fields.Float(string='Total', store=True, readonly=True, compute='_amount_all', track_visibility='always', track_sequence=6)

    @api.depends('order_line.price_total')
    def _amount_all(self):
        """
        Compute the total amounts of the SO.
        """
        for order in self:
            amount_untaxed = amount_tax = 0.0
            for line in order.order_line:
                amount_untaxed += line.price_subtotal
                amount_tax += line.price_tax
            order.update({
                'amount_untaxed': amount_untaxed,
                'amount_tax': amount_tax,
                'amount_total': amount_untaxed + amount_tax,
            })

    warehouse_id = fields.Many2one(comodel_name="stock.warehouse", string="WareHouse",required=True )
    total_amount = fields.Float(string="Total Amount",  required=False,compute="_compute_total_amount" )
    def _compute_total_amount(self):
        for rec in self:
            if rec.order_line:
                total = 0
                for line in rec.order_line:
                    total+=line.price_total

                rec.total_amount = total
            else:
                rec.total_amount = 0

    def cancel_so_pipeline(self):
        pipelines = self.env['crm.lead'].search(
            [('stage_id.probability', '=', 100.00), ('type', '=', 'opportunity')])
        for p in pipelines:
            sales = self.env['sale.order'].search([('opportunity_id', '=', p.id),('state', '=', 'sale'),('invoice_status','not in',['invoiced','to invoice'])])
            for s in sales:

                timedelta = fields.Datetime.now() - p.confirm_data

                hour1 = timedelta.days * 24
                diff = -1 * (timedelta.days)
                hours = (timedelta.seconds) / 3600

                if timedelta.days > 1:
                    # if timedelta.seconds > 86400:
                        for line in s.order_line:
                            if line.product_uom_qty != line.qty_delivered:
                                try:
                                    s.action_cancel()
                                except:
                                    break

                                # edit 22/2/2021
                                # add manager
                                team = self.env['crm.team'].search(
                                    [('member_ids', '=', p.create_uid.id)], limit=1)
                                if team:
                                    ids_manager = []
                                    for manager in team.manager_ids:
                                        title = _("Order Call Center")
                                        message = _("Check Pipline now Number %s") % p.name
                                        self.env['bus.bus'].sendone(
                                            (self._cr.dbname, 'res.partner', manager.partner_id.id),
                                            {'type': 'user_connection', 'title': title,
                                             'message': message, 'partner_id': manager.partner_id.id}
                                        )
                                        ids_manager.append(manager.partner_id.id)
                                    p.message_post(
                                        subject=('Order Cames From Call Center'),
                                        body=('Please Check This {}').format(
                                            p.name),
                                        # partner_ids=[t.team_id.team_head.partner_id.id],
                                        partner_ids=ids_manager,
                                    )

                                # end add
                                p.message_post(
                                    subject=('Order Cames From Call Center'),
                                    body=('Please Check This {}').format(
                                        p.name),
                                    # partner_ids=[t.team_id.team_head.partner_id.id],
                                    partner_ids=[p.create_uid.partner_id],
                                )

                                title = _("Order Call Center")
                                message = _("Check Pipline now Number %s") % p.name
                                self.env['bus.bus'].sendone(
                                    (self._cr.dbname, 'res.partner', p.create_uid.partner_id.id),
                                    {'type': 'user_connection', 'title': title,
                                     'message': message, 'partner_id': p.create_uid.partner_id.id}
                                )
                                stage = self.env['crm.stage'].search(
                                    [('probability', '=', 1.00)], limit=1)
                                # p.action_set_lost_new
                                p.stage_id = stage.id
                                p.stage_canceled = fields.Datetime.now()
                                self._cr.commit()
                                break

    reason = fields.Text(string="Reason Back", required=False, )
    @api.model_create_multi
    def create(self, vals):
        if not self.env.user.has_group('tradeline_crm_customize.group_disable_crm_options'):
            raise UserError("Not Allowed For Create Lead")
        for val in vals:
            val['name'] = self.env['ir.sequence'].next_by_code('crm.lead.seq')
        contracts = super(CrmLead, self).create(vals)
        return contracts

    def action_set_won_rainbowman(self):
        res = super(CrmLead, self).action_set_won_rainbowman()

        leads = self.env['crm.lead'].browse(self._context.get('active_ids', []))
        for rec in self:

            rec.stage_won = fields.Datetime.now()

            team = self.env['crm.team'].search(
                [('member_ids', '=', rec.create_uid.id)], limit=1)
            if team:
                ids_manager = []
                for manager in team.manager_ids:
                    title = _("Order Call Center (Admin)")
                    message = _("Check Pipline now Number (Admin) %s") % rec.name
                    self.env['bus.bus'].sendone(
                        (self._cr.dbname, 'res.partner', manager.partner_id.id),
                        {'type': 'user_connection', 'title': title,
                         'message': message, 'partner_id': manager.partner_id.id}
                    )
                    ids_manager.append(manager.partner_id.id)
                rec.message_post(
                    subject=('Order Cames From Call Center (Admin)'),
                    body=('(Admin) Please Check This {}').format(
                        rec.name),
                    # partner_ids=[t.team_id.team_head.partner_id.id],
                    partner_ids=ids_manager,
                )

            # end add
            rec.message_post(
                            subject=('Order Cames From Call Center'),
                            body=('Please Check This {}').format(
                                rec.name),
                            # partner_ids=[t.team_id.team_head.partner_id.id],
                            partner_ids=[rec.create_uid.partner_id.id],
                        ).sudo()
            title = _("pportunity Is Won")
            message = _("Check Pipline now Number %s") % rec.name
            rec.env['bus.bus'].sendone(
                (rec._cr.dbname, 'res.partner', rec.create_uid.partner_id.id),
                {'type': 'user_connection', 'title': title,
                 'message': message, 'partner_id': rec.create_uid.partner_id.id}
            )
            eamil_to_agent_won_template_id = rec.company_id.eamil_to_agent_won_template_id
            if eamil_to_agent_won_template_id:
                eamil_to_agent_won_template_id.sudo().send_mail(rec.id, force_send=True,
                                                          notif_layout='mail.mail_notification_light')
        return res

    @api.onchange('stage_id')
    def onchange_method_stage_id(self):
        for rec in self:
            if rec.create_uid:
                rec.message_post(
                    subject=('Order Cames From Call Center'),
                    body=('Please Check This {}').format(
                        self.name),
                    # partner_ids=[t.team_id.team_head.partner_id.id],
                    partner_ids=[rec.create_uid.partner_id.id],
                ).sudo()


    def action_sale_quotations_new(self):
        return self.create_sale_order()

    def create_sale_order(self):
        sale_obj = self.env['sale.order']
        user = self.env.user

        sales_rep = self.env['sales.rep'].search([('is_call_center','=',True)],limit=1)
        lines = []
        for line in self.order_line:
            lines.append((0, 0, {
                'product_id': line.product_id.id,
                'name': line.name,
                'product_uom_qty': line.product_uom_qty,
                'product_uom': line.product_uom.id,
                'price_unit': line.price_unit,
                'discount': line.discount,
                'salesman_id': self.user_id.id,


            }))
        sale_order = sale_obj.sudo().create({
            'partner_id': self.partner_id.id,
            'team_id': self.team_id.id,
            'user_id': self.user_id.id,
            'branch_id': self.branch_id.id,
            'campaign_id': self.campaign_id.id,
            'medium_id': self.medium_id.id,
            'origin': self.name,
            'source_id': self.source_id.id,
            'opportunity_id': self.id,
            'inv_type':'invoice',
            'warehouse_id':self.warehouse_id.id,
            'sales_rep_id':sales_rep.id if sales_rep else self.sales_rep_id.id,
            'reference_number':'Call center',
            'sales_rep_domain': "[('branch_id','='," + str(self.branch_id.id) + ")]",
            'discount_domain': "[('branches_ids','in'," + str(self.branch_id.id) + "),('state','=','run')]",
            'order_line': lines
            #     'name': name,
            #     'origin': order.name,
            #     'account_id': account_id,
            #     'price_unit': amount,
            #     'quantity': 1.0,
            #     'discount': 0.0,
            #     'uom_id': self.product_id.uom_id.id,
            #     'product_id': self.product_id.id,
            #     'sale_line_ids': [(6, 0, [so_line.id])],
            #     'invoice_line_tax_ids': [(6, 0, tax_ids)],
            #     'account_analytic_id': order.analytic_account_id.id,
            #     'printed_brand_id': so_line.printed_brand_id,
            #     'printed_unit_measure': so_line.printed_unit_measure,
            # })],
        })

        return {
            'name': _('Sale Order From Crm'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'sale.order',
            'view_id': self.env.ref('sale.view_order_form').id,
            'target': 'current',
            'res_id': sale_order.id,
        }

    def convert_to_lead(self):
        stage = self.env['crm.stage'].search(
            [('probability', '=', 0.00)], limit=1)
        return self.write({'type': 'lead','stage_id':False})

    def action_set_lost(self):
        """ Lost semantic: probability = 0, active = False """
        stage = self.env['crm.stage'].search(
            [('probability', '=', 0.00)], limit=1)
        return self.write({'probability': 0, 'active': False
                           ,'stage_id':stage.id})


    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        self.ensure_one()
        # set default value in context, if not already set (Put stage to 'new' stage)
        context = dict(self._context)
        context.setdefault('default_type', 'lead')
        # context.setdefault('default_team_id', self.team_id.id)
        # Set date_open to today if it is an opp
        default = default or {}
        # default['date_open'] = fields.Datetime.now() if self.type == 'opportunity' else False
        # Do not assign to an archived user
        default['type'] = 'lead'
        default['user_id'] = self.env.uid
        default['probability'] = 0.0
        default['reason'] = ''


        return super(CrmLead, self.with_context(context)).copy(default=default)


    # new_field = fields.Datetime(string="", required=False, )
    # stage_lead = fields.Datetime(string="Date Lead", required=False, )
    stage_pipeline = fields.Datetime(string="Date Pipeline", required=False,readonly=True )
    stage_won = fields.Datetime(string="Date Won", required=False,readonly=True )
    stage_lost = fields.Datetime(string="Date Lost", required=False, readonly=True)
    stage_canceled = fields.Datetime(string="Date Canceled", required=False,readonly=True )
    stage_refund = fields.Datetime(string="Date Refund", required=False,readonly=True )


    form_url = fields.Char('Form Url', compute='_compute_form_url')

    def _compute_form_url(self):
        if self:
            base_url = self.env['ir.config_parameter'].sudo(
            ).get_param('web.base.url')
            url_str = ''
            action = self.env.ref('tradeline_crm_customize.crm_lead_opportunities_tree_view_all_pipeline1').id
            if base_url:
                url_str += str(base_url)+'/web#'
            for rec in self:
                url_str += 'id='+str(rec.id)+'&action='+str(action) + \
                    '&model=crm.lead&view_type=form'
                rec.form_url = url_str


    def canceled_sale_order(self):

            sales = self.env['sale.order'].search(
                [('opportunity_id', '=', self.id),('state', '=', 'sale')])


            for s in sales:
                    for line in s.order_line:

                        if line.product_uom_qty != line.qty_delivered:
                            s.action_cancel()
                            # edit 22/2/2021
                            # add manager
                            team = self.env['crm.team'].search(
                                [('member_ids', '=', self.create_uid.id)], limit=1)
                            if team:
                                ids_manager = []
                                for manager in team.member_ids:
                                    title = _("Order Call Center")
                                    message = _("Check Pipline now Number %s") % self.name
                                    self.env['bus.bus'].sendone(
                                        (self._cr.dbname, 'res.partner', manager.partner_id.id),
                                        {'type': 'user_connection', 'title': title,
                                         'message': message, 'partner_id': manager.partner_id.id}
                                    )
                                    ids_manager.append(manager.partner_id.id)
                                self.message_post(
                                    subject=('Order Cames From Call Center'),
                                    body=('Please Check This {}').format(
                                        self.name),
                                    # partner_ids=[t.team_id.team_head.partner_id.id],
                                    partner_ids=ids_manager,
                                )

                            # end add
                            self.message_post(
                                subject=('Order Cames From Call Center'),
                                body=('Please Check This {}').format(
                                    self.name),
                                # partner_ids=[t.team_id.team_head.partner_id.id],
                                partner_ids=[self.create_uid.partner_id],
                            )

                            title = _("Order Call Center")
                            message = _("Check Pipline now Number %s") % self.name
                            self.env['bus.bus'].sendone(
                                (self._cr.dbname, 'res.partner', self.create_uid.partner_id.id),
                                {'type': 'user_connection', 'title': title,
                                 'message': message, 'partner_id': self.create_uid.partner_id.id}
                            )
                            stage = self.env['crm.stage'].search(
                                [('probability', '=', 1.00)], limit=1)
                            # self.action_set_lost_new
                            self.stage_id = stage.id
                            self.stage_canceled = fields.Datetime.now()
                            eamil_to_branch_canceled_template_id = self.company_id.eamil_to_agent_lost_template_id
                            if eamil_to_branch_canceled_template_id:
                                eamil_to_branch_canceled_template_id.sudo().send_mail(self.id, force_send=True,
                                                                                 notif_layout='mail.mail_notification_light')
                            break


class CrmLeadLine(models.Model):
    _name = 'crm.lead.line'

    order_id = fields.Many2one('crm.lead',
                               string='Order Reference',
                               required=True,
                               ondelete='cascade',
                               index=True,
                               copy=False)
    name = fields.Text(string='Description', required=True)
    sequence = fields.Integer(string='Sequence', default=10)
    price_unit = fields.Float('Unit Price',readonly=True, required=True, digits=dp.get_precision('Product Price'), default=0.0,store=True)
    price_subtotal = fields.Float(compute='_compute_amount', string='Subtotal', readonly=True, store=True)
    price_tax = fields.Float(compute='_compute_amount', string='Total Tax', readonly=True, store=True)
    price_total = fields.Float(compute='_compute_amount', string='Total', readonly=True, store=True)
    price_reduce = fields.Float(compute='_get_price_reduce', string='Price Reduce',
                                digits=dp.get_precision('Product Price'), readonly=True, store=True)

    def _default_tax_id(self):

        return  self.env.company.account_sale_tax_id

    tax_id = fields.Many2many('account.tax', string='Taxes',
                              domain=['|', ('active', '=', False), ('active', '=', True)],default=_default_tax_id)
    price_reduce_taxinc = fields.Float(compute='_get_price_reduce_tax', string='Price Reduce Tax inc', readonly=True,
                                          store=True)
    price_reduce_taxexcl = fields.Float(compute='_get_price_reduce_notax', string='Price Reduce Tax excl',
                                           readonly=True, store=True)

    discount = fields.Float(string='Discount (%)', digits=dp.get_precision('Discount'), default=0.0,)

    product_id = fields.Many2one('product.product', string='Product', domain=[('sale_ok', '=', True)],
                                 change_default=True, ondelete='restrict')

    @api.onchange('product_id')
    def onchange_method_product_id(self):
        self.price_unit = self.product_id.lst_price

    product_uom_qty = fields.Float(string='Ordered Quantity', digits=dp.get_precision('Product Unit of Measure'),
                                   required=True, default=1.0)
    product_uom = fields.Many2one('uom.uom', string='Unit of Measure')
    product_image = fields.Binary('Product Image', related="product_id.image_1920", store=False, readonly=False)

    salesman_id = fields.Many2one(related='order_id.user_id', store=True, string='Salesperson', readonly=True)
    company_id = fields.Many2one(related='order_id.company_id', string='Company', store=True, readonly=True)
    order_partner_id = fields.Many2one(related='order_id.partner_id', store=True, string='Customer', readonly=False)
    display_type = fields.Selection([
        ('line_section', "Section"),
        ('line_note', "Note")], default=False, help="Technical field for UX purpose.")
    amount_dis = fields.Float(string='Amount Discount')


    @api.onchange('amount_dis', 'price_unit')
    def on_change_amount_dis(self):
        for rec in self:
            if rec.price_unit:
                rec.discount = (rec.amount_dis / rec.price_unit) * 100

    @api.depends('price_subtotal', 'product_uom_qty')
    def _get_price_reduce_notax(self):
        for line in self:
            line.price_reduce_taxexcl = line.price_subtotal / line.product_uom_qty if line.product_uom_qty else 0.0

    @api.depends('price_total', 'product_uom_qty')
    def _get_price_reduce_tax(self):
        for line in self:
            line.price_reduce_taxinc = line.price_total / line.product_uom_qty if line.product_uom_qty else 0.0

    @api.depends('price_unit', 'discount')
    def _get_price_reduce(self):
        for line in self:
            line.price_reduce = line.price_unit * (1.0 - line.discount / 100.0)

    @api.depends('product_uom_qty', 'discount', 'price_unit', 'tax_id')
    def _compute_amount(self):
        """
        Compute the amounts of the SO line.
        """
        for line in self:
            price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            taxes = line.tax_id.compute_all(price, False, line.product_uom_qty,
                                            product=line.product_id, partner=None)
            line.update({
                'price_tax': sum(t.get('amount', 0.0) for t in taxes.get('taxes', [])),
                'price_total': taxes['total_included'],
                'price_subtotal': taxes['total_excluded'],
            })

    @api.onchange('product_id')
    def product_id_change(self):
        if not self.product_id:
            return {'domain': {'product_uom': []}}

        self.update({
            'name': self.product_id.name,
            'product_uom': self.product_id.uom_id.id,
        })

    @api.onchange('product_id','discount_id','unit_price')
    def onchange_method_discount_id(self):
        return {'domain': {'discount_id': [('categ_id', '=',self.product_id.categ_id.id)]}}



    available_qty = fields.Float(string="Available QTY (Branch)",  required=False,compute="_compute_avaliable_qty" )
    reserved_qty = fields.Float(string="Reserved QTY (CRM)",  required=False,compute="_compute_avaliable_qty" )

    def _compute_avaliable_qty(self):
        for rec in self:
            if rec.order_id.warehouse_id.id != False:
                stock_quant_quantity = sum(self.env['stock.quant'].search([('location_id', '=', rec.order_id.warehouse_id.lot_stock_id.id),
                                                              ('product_id', '=', rec.product_id.id)]).mapped('quantity'))
                stock_quant_reserved_quantity = sum(
                    self.env['stock.quant'].search([('location_id', '=', rec.order_id.warehouse_id.lot_stock_id.id),
                                                    ('product_id', '=', rec.product_id.id)]).mapped('reserved_quantity'))

                stock_quant_reserved_quantity_by_crm = sum(
                    self.env['crm.lead.line'].search([('id','!=',rec.id),('order_id.type','=','opportunity'),('order_id.stage_id','=',1),('order_id.warehouse_id', '=', rec.order_id.warehouse_id.id),
                                                    ('product_id', '=', rec.product_id.id)]).mapped(
                        'product_uom_qty'))



                rec.available_qty = stock_quant_quantity - ( stock_quant_reserved_quantity + stock_quant_reserved_quantity_by_crm)
                rec.reserved_qty = stock_quant_reserved_quantity_by_crm
            else:
                rec.available_qty = 0



    @api.onchange('product_id')
    def onchange_product_new(self):
        # for rec in self:
        if self.product_id:
            if self.order_id.warehouse_id.lot_stock_id:
                stock_quant_on_hand = sum(self.env['stock.quant'].search([('location_id', '=', self.order_id.warehouse_id.lot_stock_id.id),
                                                                  ('product_id', '=', self.product_id.id)]).mapped('quantity'))

                stock_quant_reservd = sum(
                    self.env['stock.quant'].search([('location_id', '=', self.order_id.warehouse_id.lot_stock_id.id),
                                                    ('product_id', '=', self.product_id.id)]).mapped('reserved_quantity'))
                stock_quant = stock_quant_on_hand -stock_quant_reservd
                if stock_quant:
                    # for quant in stock_quant:
                        if stock_quant <= 0:
                            warning_mess = {
                                'title': _('Not enough inventory!'),
                                'message': 'Insufficient Product Quantity'
                            }
                            # return {'warning': warning_mess}
                            raise ValidationError(_("Not enough inventory!\ Insufficient Product Quantity."
                            ))
                        else:
                            stock_quant_reserved_quantity_by_crm = sum(
                                self.env['crm.lead.line'].search([('order_id.stage_id', '=', 1), ( 'order_id.warehouse_id', '=', self.order_id.warehouse_id.id),
                                                                  ('product_id', '=', self.product_id.id)]).mapped('product_uom_qty'))
                            ava = stock_quant -stock_quant_reserved_quantity_by_crm

                            if ava <= 0:
                                warning_mess = {
                                    'title': _('Not enough inventory! (CRM)'),
                                    'message': 'Insufficient Product Quantity'
                                }
                                # return {'warning': warning_mess}
                                raise ValidationError(_("Not enough inventory! (CRM) \n Insufficient Product Quantity." ))

                else:
                    warning_mess = {
                        'title': _('Not enough inventory!'),
                        'message': 'Insufficient Product Quantity, No Product In Source Location'
                    }
                    return {'warning': warning_mess}
            else:
                stock_quant2 = self.env['stock.quant'].search([('quantity', '>',0),('product_id', '=', self.product_id.id),('location_id.usage', '=', 'internal')]).mapped("location_id")

                stock_warehouse = self.env['stock.warehouse'].search([('lot_stock_id', 'in',stock_quant2.ids)]).mapped("name")

                warning_mess = {
                    'title': _('Available inventory'),
                    'message': str(stock_warehouse)
                }
                return {'warning': warning_mess}
        # return {}


    @api.constrains('product_id','product_uom_qty')
    def product_id_con(self):
        for rec in self:
            if rec.product_id:
                if rec.product_uom_qty > rec.available_qty:
                                raise ValidationError(_("Not enough inventory! (CRM) \n Insufficient Product Quantity."))

    # @api.model
    # def create(self, vals):
    #     print("vals" , vals)
    #     if 'order_id' in vals:
    #         crm_lead = self.env['crm.lead'].browse(vals['order_id'])
    #         if 'product_id' in vals:
    #
    #     result = super(CrmLeadLine, self).create(vals)
    #     return result
    item_code = fields.Char(string='Item Code', related='product_id.barcode', store=True)



# class Lead2OpportunityPartner(models.TransientModel):
#     _inherit = 'crm.lead2opportunity.partner'
#     _name = 'crm.lead2opportunity.partner'
#
#     def _compute_selection(self):
#         if self.env.user.has_group('tradeline_crm_customize.group_crm_selections'):
#             selection_options = [
#                 ('exist', 'Link to an existing customer'),
#                 ('create', 'Create a new customer'),
#                 ('nothing', 'Do not link to a customer')
#             ]
#         else:
#             selection_options = [
#                 ('nothing', 'Do not link to a customer')
#             ]
#         return selection_options
#
#     @api.depends('lead_id')
#     def _compute_action(self):
#         for convert in self:
#             if not convert.lead_id:
#                 convert.action = 'nothing'
#             else:
#                 partner = convert.lead_id._find_matching_partner()
#                 if partner:
#                     convert.action = 'exist'
#                 elif convert.lead_id.contact_name:
#                     convert.action = 'create'
#                 else:
#                     convert.action = 'nothing'

    # @api.multi
    # def compute_selection_top(self):
    #     if self.env.user.has_group('tradeline_crm_customize.group_crm_selections'):
    #         selection_options = [
    #             ('convert', 'Convert to opportunity'),
    #             ('merge', 'Merge with existing opportunities')
    #         ]
    #     else:
    #         selection_options = [
    #             ('convert', 'Convert to opportunity'),
    #         ]
    #     return selection_options

    actionn = fields.Selection('_compute_selection', 'Related Customer', required=True, default='nothing')
    # name = fields.Selection('compute_selection_top', 'Conversion Action', required=True, default='convert')


