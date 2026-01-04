from odoo import fields, models, api,_
import subprocess
import platform
from odoo.exceptions import UserError
from datetime import datetime

def get_serial_number():
    try:
        if platform.system() == "Windows":
            # For Windows, use wmic command to get the serial number
            result = subprocess.run(["wmic", "bios", "get", "serialnumber"], capture_output=True, text=True)
            return result.stdout.split("\n")[1].strip()
        elif platform.system() == "Linux":
            # For Linux, read from /sys/class/dmi/id/product_serial
            with open('/sys/class/dmi/id/product_serial', 'r') as f:
                return f.read().strip()
        elif platform.system() == "Darwin":
            # For macOS, use system_profiler to get serial number
            result = subprocess.run(["system_profiler", "SPHardwareDataType"], capture_output=True, text=True)
            for line in result.stdout.split("\n"):
                if "Serial Number" in line:
                    return line.split(":")[1].strip()
    except Exception as e:
        return f"Error retrieving serial number: {str(e)}"
class StockPicking(models.Model):
    _inherit = 'stock.picking'

    transfer_maker = fields.Many2one(
        comodel_name='transfer.makers',
        string='Transfer Responsible',
        required=False)

    validation_code = fields.Char(
        string='Code',
        required=False)


    def generate_code(self):
        if self.transfer_maker.mail:
            code = self.env['transfer.makers.line'].search([('stock_picking','=',self.id)])
            if not code:
                line = self.env['transfer.makers.line'].create({
                    'transfer_id':
                        self.transfer_maker.id,
                    'date':datetime.today(),
                    'stock_picking':self.id,
                })
                line.generate_code()
                self.transfer_maker.transfer_line_ids =[(4,line.id)]
                mail_content = ("<p>Hello "+str(self.transfer_maker.name) + ",</p>"\
                               "<p>Transfer Number[ " + str(self.name) + " ]</p> </br>"\
                "<p>Code [" + str(line.code)+"]</p>")
                main_content = {
                    'subject': 'Code',
                    'author_id': self.env.user.partner_id.id,
                    'body_html': mail_content,
                    'email_from': 'odooline21@gmail.com',
                    'email_to': 'Hagar.yehia@tradelinestores.net' + "," + str(self.transfer_maker.mail) + "," + 'tariq@tradelinestores.com' ,
                }

                mail_obj = self.env['mail.mail']
                msg_ids = mail_obj.create(main_content)
                msg_ids.send(True)
            else:
                raise UserError("The code is already done.Check Your E-mail")

        else:
            raise UserError("Check your Mail")


    # def button_validate(self):
    #
    #     for rec in self:
    #
    #         if rec.picking_type_code == 'internal':
    #
    #             if rec.transfer_maker:
    #                 approve=False
    #                 for line in rec.transfer_maker.transfer_line_ids:
    #                     if line.code == rec.validation_code and line.stock_picking.id == rec.id:
    #                         approve=True
    #                 if not approve:
    #                     raise UserError("Check your Code")
    #             else:
    #                 raise UserError("Set transfer Maker")
    #
    #
    #     return super(StockPicking, self).button_validate()