from distutils.command.check import check

from odoo import models, fields, api,_
from odoo.exceptions import UserError
from odoo.tools import float_is_zero, float_compare, DEFAULT_SERVER_DATETIME_FORMAT
from datetime import datetime

class TransferLine(models.Model):
    _name = 'transfer.request.line'

    product_id = fields.Many2one(
        comodel_name='product.product',
        string='Product',
        required=True)

    tracking = fields.Selection(
        string='',
        related='product_id.tracking',
        required=False, )

    item_code = fields.Char(related='product_id.barcode')


    qty = fields.Float(
        string='Qty',
        required=False)

    request_id = fields.Many2one(
        comodel_name='transfer.request',
        string='Request',
        required=False)
    serial_domain = fields.Char(
        string='serial_domain',
        required=False)
    serial_ids = fields.Many2many(
        comodel_name='stock.lot',
        string='Serial',
        required=False)

    @api.onchange('qty')
    def _onchange_product_id(self):
        if self.product_id.tracking == 'serial' and self.qty != len(self.serial_ids.ids):
            raise UserError("Max Qty %s For %s !!" % (str(len(self.serial_ids.ids)), str(self.product_id.name)))



    @api.onchange('request_id.from_location','product_id')
    def _get_domain(self):
        for rec in self:
            ids=[]
            if rec.tracking =='serial':
                serials = self.env['stock.quant'].search([('product_id','=',rec.product_id.id),('location_id','=',rec.request_id.from_location.id),('quantity','=',1),('reserved_quantity','=',0)]).mapped('lot_id')
                for serial in serials:
                    ids.append(serial.id)
                rec.serial_domain ="[('id','in',"+str(ids)+")]"
            else:
                rec.serial_domain ="[('id','=',0)]"





class TransferRequest(models.Model):
    _name = 'transfer.request'
    _inherit = ['mail.thread','mail.activity.mixin','base.transfer']
    _description = "Transfer Request"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('transfer.request') or _('New')
        return super().create(vals_list)

    name = fields.Char(copy=False,readonly=True)

    # @api.model
    # def read_group(self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True):
    #     """
    #         Override read_group to calculate the sum of the non-stored fields that depend on the user context
    #     """
    #
    #     if  self.env.user.has_group('tradeline_warehouse_transfer.group_transfer_user'):
    #         warehouse = self.env['stock.warehouse'].search()
    #         if self.env.user.id not in warehouse.users_ids.ids:
    #         domain += [('invoice_date', '>=', '2024-1-1')]
    #     return super(TransferRequest, self).read_group(domain, fields, groupby, offset=offset, limit=limit,
    #                                                       orderby=orderby, lazy=lazy)
    #
    # @api.model
    # def search_read(self, domain=None, fields=None, offset=0, limit=None, order=None):
    #     if  self.env.user.has_group('tradeline_warehouse_transfer.group_transfer_user'):
    #         domain += [('invoice_date', '>=', '2024-1-1')]
    #     res = super(TransferRequest, self).search_read(
    #         domain, fields, offset, limit, order)
    #     return res
    # company_id = fields.Many2one(copy=False,
    #     comodel_name='res.company',default=lambda self: self.env.company,
    #     string='Company',
    #     required=True)

    def create_line(self, account_id,  debit_value, credit_value, name,account_analytic_id):

        line = (0, 0, {
            'account_id': account_id,
            'credit': credit_value,
            'debit': debit_value,
            'analytic_distribution':account_analytic_id.analytic_distribution if account_analytic_id else False,
            'name': name,
        })
        return line

    user_id = fields.Many2one('res.users', 'Responsible', default=lambda self: self.env.user, )


    def create_transfer_journal_entry(self,  total, name,journal,debit_account,credit_account,from_warehouse,to_warehouse):
        debit_account_analytic_id=False
        credit_account_analytic_id=False
        if self.is_pos_mrp_order_installed():
            debit_account_analytic_id = self.env['account.analytic.distribution.model'].search(
                [('warehouse_id', '=', to_warehouse.id)])
            credit_account_analytic_id = self.env['account.analytic.distribution.model'].search(
                [('warehouse_id', '=', from_warehouse.id)])

        line_name = str(name)
        debit_line = self.create_line(debit_account,total,0, line_name,debit_account_analytic_id)
        credit_line = self.create_line(credit_account,0, total,line_name,credit_account_analytic_id )
        line_data = []
        line_data.append(debit_line)
        line_data.append(credit_line)
        move = self.env['account.move'].create({
            'ref': name+"- Transfer Request",
            'journal_id': journal,
            'line_ids': line_data
        })

        move.action_post()
    def _get_default_warehouse(self):
        return self.env['stock.warehouse'].search([('default_transfer','=',True)]).id
    from_warehouse = fields.Many2one(
        comodel_name='stock.warehouse',
        string='Source warehouse',tracking=True,
        required=True)

    from_location = fields.Many2one(
        comodel_name='stock.location',related="from_warehouse.lot_stock_id",
        string='Location From',
        required=False)

    to_warehouse = fields.Many2one(
        comodel_name='stock.warehouse',tracking=True,
        string='Receiving warehouse', default=_get_default_warehouse,
        required=True)

    to_location = fields.Many2one(
        comodel_name='stock.location',related="to_warehouse.lot_stock_id",
        string='Location TO',
        required=False)

    state = fields.Selection(
        string='State',copy=False,track_visibility='onchange',
        selection=[('draft', 'Draft'),
                   ('approved', 'Approved'),('create','Created'),('in_progress', 'In Progress'),('received', 'Received'),('completed', 'Transfer Completed'), ('cancel', 'Canceled'), ],
        required=False, default='draft')

    date = fields.Date(
        string='Date',default=fields.date.today(),
        required=False)

    create_entry = fields.Boolean(
        string='Create_entry',
        required=False)
    lines = fields.One2many(
        comodel_name='transfer.request.line',
        inverse_name='request_id',
        string='Lines',
        required=False)
    lot_id = fields.Many2many('stock.lot', 'Lot/Serial Number', related='lines.serial_ids',
                             readonly=False)

    transfer_ids = fields.Many2many(
        comodel_name='stock.picking',copy=False,
        string='Back_orders')

    stock_journal = fields.Many2one(
        comodel_name='account.journal', domain=[('type', '=', 'general')],
        readonly=False,
        string='Stock Journal',
        required=False)

    debit_account = fields.Many2one(
        comodel_name='account.account', readonly=False,
        string='Debit Account',
        required=False)

    credit_account = fields.Many2one(
        comodel_name='account.account', readonly=False,
        string='Credit Account',

        required=False)



    sales_rep = fields.Many2one (comodel_name="sales.rep", string="Sales Rep", required=True, )

    #   total_cost = fields.Float(
    #  string='Total Transfer Cost',
    #  required=False)

    def create_first_transfer(self):

        lines= []
        serials ={}
        qty={}
        products=[]
        for line in self.lines:
            if str(line.product_id.id) not in qty.keys():
                qty.setdefault(str(line.product_id.id), line.qty)
            else:
                qty[str(line.product_id.id)]  += line.qty

        for line in self.lines:
            if line.product_id.id not in products:
                products.append(line.product_id.id)
                if line.qty == 0:
                    raise UserError("Set Qty For %s !!"%str(line.product_id.name))
                lines.append((0,0,{
                    'product_id':line.product_id.id,
                    'branch_id':self.from_warehouse.branch_id.id,
                    'name': line.product_id.name,
                    'product_uom_qty':qty[str(line.product_id.id)],
                    'product_uom':line.product_id.uom_id.id,
                    'location_id': self.sudo().from_location.id,
                    'location_dest_id': self.to_warehouse.wh_input_stock_loc_id.id,
                    'location_final_id': self.to_warehouse.wh_input_stock_loc_id.id,
                }))
            if line.product_id.tracking == 'serial':
                for serial in line.serial_ids:
                    if str(serial.id) not in serials.keys():
                        serials.setdefault(str(serial.id),line.product_id.id)
                    else:
                        raise UserError("Remove " + str(serial.name))

        # operation_type = self.env['stock.picking.type'].search([('warehouse_id','=',self.from_warehouse.id),('code','=','internal')])
        date = datetime.combine(self.date, datetime.min.time())
        transfer = {
            'branch_id': self.from_warehouse.branch_id.id,
            'picking_type_id' : self.from_warehouse.int_type_id.id,
            'location_id' : self.sudo().from_location.id,
            'location_dest_id' : self.to_warehouse.wh_input_stock_loc_id.id,
            'scheduled_date' :date ,
            'move_ids_without_package': lines,
            'picking_type_code': 'internal',
            'request_id' : self.id,
        }
        transfer = self.env['stock.picking'].sudo().create(transfer)
        transfer.action_assign()
        for line in transfer.move_line_ids_without_package:

            for serial , product in serials.items():

                if line.product_id.id  == product and serials[serial] != False  :
                    line.lot_id  = int(serial)
                    line.qty_done = 1
                    line.quantity=1
                    serials[serial] = False
                    break
        return transfer.id


    def create_second_transfer(self):

        lines= []
        serials= {}
        qty = {}
        products = []
        for line in self.lines:
            if str(line.product_id.id) not in qty.keys():
                qty.setdefault(str(line.product_id.id), line.qty)
            else:
                qty[str(line.product_id.id)] += line.qty

        for line in self.lines:
            if line.product_id.id not in products:
                products.append(line.product_id.id)
                lines.append((0,0,{
                    'product_id':line.product_id.id,
                    'branch_id': self.to_warehouse.branch_id.id,
                    'name': line.product_id.name,
                    'product_uom_qty':qty[str(line.product_id.id)],
                    'product_uom':line.product_id.uom_id.id,
                    'location_id' : self.to_warehouse.wh_input_stock_loc_id.id,
                    'location_dest_id': self.to_location.id,
                    'location_final_id': self.to_location.id,
                }))
            if line.product_id.tracking == 'serial':
                if not  line.serial_ids:
                    raise UserError("Add Serial")

            # if str(line.serial_id.id) not in serials.keys():
            #     serials.setdefault(str(line.serial_id.id), line.product_id.id)
            # else:
            #     raise UserError("Remove " % str(line.serial_id.name))
        # operation_type = self.env['stock.picking.type'].search([('warehouse_id','=',self.to_warehouse.id),('code','=','internal')])
        date=datetime.combine(self.date, datetime.min.time())

        transfer={
            'picking_type_id': self.to_warehouse.int_type_id.id,
            'branch_id': self.to_warehouse.branch_id.id,
            'location_id' : self.to_warehouse.wh_input_stock_loc_id.id,
            'location_dest_id' : self.to_location.id,
            'scheduled_date': date,
            'move_ids_without_package':lines,
            'picking_type_code': 'internal',
            'request_id':self.id
        }
        transfer = self.env['stock.picking'].sudo().create(transfer)

        return transfer.id

    @api.model
    def search_fetch(self, domain, field_names, offset=0, limit=None, order=None):
        domain += ['|', ('to_warehouse.branch_id', 'in', self.env.user.branch_ids.ids),('from_warehouse.branch_id', 'in', self.env.user.branch_ids.ids)]

        return super().search_fetch(domain, field_names, offset, limit, order)
    def check_qty(self):

        for line in self.lines:
            if line.qty > 0:
                qty = sum(self.env['stock.quant'].search([('product_id','=',line.product_id.id),('location_id','=',self.from_location.id)]).mapped('quantity'))
                if qty < line.qty or not qty:
                   raise UserError("Not Have Qty For "+line.product_id.name +" at "+self.from_warehouse.name)
            else:
                raise UserError("Set Quantity" )

    def set_draft(self):
        self.state = 'draft'

    def create_request(self):


            self.transfer_ids = [(6,0, [self.create_first_transfer(),self.create_second_transfer()])]

            self.state = 'create'
            self._tradeline_refresh_source_documents()

    def _tradeline_refresh_source_documents(self):
        for request in self:
            request.transfer_ids.filtered(
                lambda transfer: transfer.picking_type_code == 'internal' and transfer.request_id
            )._tradeline_update_source_document_from_chain()

    def approve_request(self):
        if self.lines:
            self.check_qty()
            self.state = 'approved'
        else:
            raise UserError("Set Lines!!")

    def create_enter(self,transfer_id):

        if self.stock_journal and self.debit_account and self.credit_account:
            stock_journal = self.stock_journal
            debit_account = self.debit_account
            credit_account = self.credit_account
        else:
            stock_journal = self.company_id.stock_journal
            debit_account = self.company_id.debit_account
            credit_account = self.company_id.credit_account

        total = 0
        for transfer in self.transfer_ids:
            if transfer.location_dest_id == self.to_location and transfer_id == transfer.id:

                    for line in transfer.move_ids:
                        total += line.product_id.standard_price * line.quantity_done
        if total !=0:
            self.total_cost += total
            self.create_transfer_journal_entry(total,self.name,stock_journal.id,debit_account.id,credit_account.id,self.from_warehouse,self.to_warehouse)

    transfers_count = fields.Integer(string="Transfers Count",copy=False,compute='compute_transfer')

    @api.onchange('transfer_ids')
    def compute_transfer(self):
        self.transfers_count = len(self.transfer_ids)

    def action_view_transfer(self):

        # action = self.env["ir.actions.act_window"]._for_xml_id("stock.action_picking_tree_all")
        # action['domain'] =
        return {
            "type": "ir.actions.act_window",
            "res_model": "stock.picking",
            'name':'Picking',
            "domain": [('id', 'in', self.transfer_ids.ids)],
            'view_mode':"list,form"
        }

    def action_cancel(self):

        self.state = 'cancel'

    def action_complete(self):
        if self.env.user.id == 2:
            self.state = 'completed'
        else:
            raise UserError("Not Allow Admin Only")


    def cancel(self):
        for transfer in self.transfer_ids:
            transfer.action_cancel()
            break
        self._tradeline_refresh_source_documents()

    def unlink(self):
        # Add code here

        if self.state != 'draft':

            raise UserError("Not Allow To Cancel")

        return super(TransferRequest, self).unlink()

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    request_id = fields.Many2one(
        comodel_name='transfer.request',
        string='Request',copy=True,
        required=False)

    create_entry = fields.Boolean(
        string='Create_entry',
        required=False)


    cancel_options = fields.Selection(
        string='Cancel options',
        selection=[('only', 'Only Transfer'),
                   ('request', 'Transfer And Request'), ],default='request',
        required=False, )

    @api.model
    def _tradeline_join_source_document_refs(self, references):
        ordered_refs = []
        seen = set()
        for reference in references:
            if not reference:
                continue
            reference = str(reference).strip()
            if not reference or reference in seen:
                continue
            seen.add(reference)
            ordered_refs.append(reference)
        return ",".join(ordered_refs)

    def _tradeline_get_chain_source_document_refs(self):
        self.ensure_one()
        if not self.request_id or self.picking_type_code != 'internal':
            return []

        # Prefer explicit stock move links to avoid broad location-based matches.
        linked_previous = self.move_ids_without_package.mapped('move_orig_ids.picking_id').filtered(
            lambda transfer: transfer.id != self.id
            and transfer.picking_type_code == 'internal'
            and transfer.company_id.id == self.company_id.id
            and transfer.name
        )
        if linked_previous:
            source_transfer = linked_previous.sorted(lambda transfer: transfer.id)[0]
            return [source_transfer.name]

        # Fallback for legacy data where move links are missing: use only the closest
        # previous transfer in the same request (same handover location), never next transfers.
        previous_transfers = self.request_id.transfer_ids.filtered(
            lambda transfer: transfer.id != self.id
            and transfer.picking_type_code == 'internal'
            and transfer.location_dest_id.id == self.location_id.id
            and transfer.state != 'cancel'
            and transfer.name
        )
        if not previous_transfers:
            return []
        source_transfer = previous_transfers.sorted(lambda transfer: transfer.id)[0]
        return [source_transfer.name]

    def _tradeline_update_source_document_from_chain(self):
        for rec in self:
            if not rec.request_id or rec.picking_type_code != 'internal':
                continue
            source_document = rec._tradeline_join_source_document_refs(
                rec._tradeline_get_chain_source_document_refs()
            )
            if rec.origin != source_document:
                rec.origin = source_document


    def button_validate(self):
        res = super(StockPicking, self).button_validate()
        for rec in self:
            serials =[]
            serials_dic ={}
            qty ={}
            if rec.request_id:
                if len(rec.request_id.lines) != len(rec.move_ids_without_package) and  str(rec.origin).find('Return of ') == -1:
                    raise UserError("check products must be same that in request")
                for rq_line in rec.request_id.lines:
                    check =False
                    if rq_line.product_id.tracking == 'serial':
                        for serial in rq_line.serial_ids:
                            serials.append(serial.name)
                    for line in rec.move_line_ids_without_package:
                        if rq_line.product_id.tracking == 'serial' and  rq_line.product_id.id == line.product_id.id:
                            for serial in rq_line.serial_ids:
                                if line.lot_id.id == serial.id:
                                    check = True
                                    serials.remove(serial.name)
                                    break

                        elif rq_line.product_id.tracking != 'serial' and rq_line.product_id.id == line.product_id.id:
                            if line.qty_done == rq_line.qty:
                                check = True
                                break
                    if check:
                        continue
                    elif not check and  str(rec.origin).find('Return of ') == -1:
                        if len(serials)> 0:
                            s = ','.join(str(x) for x in serials)
                            raise UserError("Check Serials "+ s)

                        # raise UserError("Check Transfer Lines and Request Lines or set  qty done for lines")
                for line in rec.request_id.lines:
                    if line.product_id.tracking == 'serial':
                        if line.serial_ids:
                            for serial in  line.serial_ids.ids:
                                if str(serial) not in serials_dic.keys():
                                    serials_dic.setdefault(str(serial), line.product_id.id)
                                else:
                                    raise UserError("Set Serials")
                    else:
                        if str(line.product_id.id) not in qty.keys():
                            qty.setdefault(str(line.product_id.id),line.qty)
                        else:
                            qty[str(line.product_id.id)] += line.qty

                if len(serials)>0:
                    for serial, product in serials_dic.items():
                        serial_check = False
                        for line in rec.move_line_ids_without_package:
                            if serial:
                                if line.lot_id.id == int(serial):
                                    serial_check = True
                        if not serial_check and  str(rec.origin).find('Return of ') == -1:
                            raise UserError("check serials")
            for order in rec.backorder_ids:
                rec.request_id.sudo().transfer_ids = [(4,order.id)]
            if rec.request_id:
                rec.request_id._tradeline_refresh_source_documents()

            if rec.request_id.from_location == rec.location_id:
                for state in rec.request_id.sudo().transfer_ids.mapped('state'):
                    if state != 'done':
                        rec.request_id.state = 'received'
                        return res

                rec.request_id.state = 'completed'
                return res

            if rec.request_id.from_location == rec.location_dest_id:
                for state in rec.request_id.sudo().transfer_ids.mapped('state'):
                    if state != 'done':
                        rec.request_id.state = 'received'
                        return res

                rec.request_id.state = 'completed'
                return res


            elif rec.request_id.to_location == rec.location_dest_id:


                if not rec.create_entry:
                    if rec.state =='done':
                        #
                        # rec.request_id.create_enter(rec.id)
                        rec.create_entry = True

                for state in  rec.backorder_ids.mapped('state'):
                    if state != 'done':
                        return res

                for state in  rec. request_id.sudo().transfer_ids.mapped('state'):
                    if state != 'done':
                        return res

                rec.request_id.state = 'completed'

        return res


    def action_cancel(self):
        return_cancel_next = bool(self.env.context.get('tradeline_return_cancel_next'))
        res = super(StockPicking, self).action_cancel()
        for rec in self:
            if return_cancel_next:
                if rec.request_id:
                    rec.request_id._tradeline_refresh_source_documents()
                continue
            if rec.cancel_options == 'only':
                if rec.request_id:
                    rec.request_id._tradeline_refresh_source_documents()
                continue
            for transfer in rec.request_id.transfer_ids:
                if transfer.state == 'done':
                    if rec.backorder_id:
                        return res
                    raise UserError("Not Allowed Cancel")
                elif  transfer.state != 'cancel':
                    transfer.action_cancel()
            if rec.request_id and rec.request_id.state!='cancel' :
                rec.request_id.action_cancel()
            if rec.request_id:
                rec.request_id._tradeline_refresh_source_documents()
        return  res

    @api.onchange('backorder_ids')
    def _onchange_backorder_ids(self):
        for rec in self.backorder_ids:
            rec.request_id = self.request_id.id

    def action_confirm(self):
        for rec in self:
            if rec.request_id.from_location == rec.location_id:
                rec.request_id.state = 'in_progress'
        return super(StockPicking, self).action_confirm()

