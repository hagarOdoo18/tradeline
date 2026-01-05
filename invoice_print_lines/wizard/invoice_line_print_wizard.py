from odoo import models, fields


class InvoiceLinePrintWizardLine(models.TransientModel):
    _name = 'invoice.line.print.wizard.line'
    _description = 'Invoice Line Print Wizard Line'

    wizard_id = fields.Many2one(
        'invoice.line.print.wizard',
        required=True,
        ondelete='cascade'
    )

    move_line_id = fields.Many2one(
        'account.move.line',
        string='Invoice Line',

    )

    name = fields.Char(
        related='move_line_id.name',
        readonly=True
    )

    quantity = fields.Float(
        related='move_line_id.quantity',
        readonly=True
    )

    price_unit = fields.Float(
        related='move_line_id.price_unit',
        readonly=True
    )

    allow_print = fields.Boolean(
        string='Allow Print'
    )

class InvoiceLinePrintWizard(models.TransientModel):
    _name = 'invoice.line.print.wizard'

    move_id = fields.Many2one('account.move', readonly=True)
    line_ids = fields.One2many(
        'invoice.line.print.wizard.line',
        'wizard_id',
        string='Invoice Lines',
    default = lambda self: self.prepare_working_invoice()
    )

    def prepare_working_invoice(self):
        account_invoice = self.env['account.move'].browse(self.env.context.get('active_ids', False))
        data = []
        if account_invoice:
            for rec in account_invoice.invoice_line_ids:
                data.append((0, 0, {
                                    'allow_print': rec.select_for_report,
                                    'name': rec.product_id.name,
                                    'quantity': rec.quantity,
                                    'price_unit': rec.price_unit,
                                    'move_line_id':rec.id,
                            }))
        return data

    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        move_id = self.env.context.get('default_move_id')

        if move_id:

            res['move_id'] = move_id

        return res

    def action_apply(self):

        # فعّل المختار فقط
        for line in self.line_ids:
            line.move_line_id.select_for_report = line.allow_print
