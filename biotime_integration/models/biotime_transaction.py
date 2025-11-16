from odoo import fields, models
from odoo import _
from datetime import datetime, timedelta
from collections import defaultdict
from odoo import models, fields, api


class BiotimeTransaction(models.Model):
    _name = 'biotime.transaction'
    _order = 'punch_time'
    _description = 'Bio Time Transaction'

    server_id = fields.Many2one('biotime.terminal', string="Server name")
    employee_id = fields.Many2one('hr.employee', string='Employé(e)')
    company_id = fields.Many2one(
        related='employee_id.company_id',
        string='Company',
        required=False)
    employee_code = fields.Char(string="Employee code")
    punch_state = fields.Selection([
        ('I', 'Check in'),
        ('O', 'Check out'),
        ('0', 'Check in0'),
        ('1', 'Check out1'),
        ('2', 'Break out'),
        ('3', 'Break in'),
        ('4', 'Overtime in'),
        ('5', 'Overtime out'),
        ('255', 'Other')
    ], string='Punch state')
    verify_type = fields.Selection([
        ('0', 'Password'),
        ('1', 'Fingerprint'),
        ('2', 'Employee ID'),
        ('3', 'Password'),
        ('4', 'Card'),
        ('5', 'Fingerprint/Password'),
        ('6', 'Fingerprint/Card'),
        ('7', 'Password/Card'),
        ('8', 'Employee ID & Fingerprint'),
        ('9', 'Fingerprint & Password'),
        ('10', 'Fingerprint & Card'),
        ('11', 'Password & Card'),
        ('12', 'Fingerprint & Password & Card'),
        ('13', 'Employee ID & Fingerprint & Password'),
        ('14', 'Fingerprint & Card & Employee ID'),
        ('15', 'Face'),
        ('16', 'Face & Fingerprint'),
        ('17', 'Face & Password'),
        ('18', 'Face & Card'),
        ('19', 'Face & Fingerprint & Card'),
        ('20', 'Face & Fingerprint & Password'),
        ('21', 'Finger Vein'),
        ('22', 'Finger Vein & Password'),
        ('23', 'Finger Vein & Card'),
        ('24', 'Finger Vein & Password & Card'),
        ('25', 'Palm'),
        ('26', 'Palm & Card'),
        ('27', 'Palm & Face'),
        ('28', 'Palm & Fingerprint'),
        ('29', 'Palm & Fingerprint & Card'),
        ('101', 'GPS'),
        ('102', 'AI Camera'),
        ('200', 'Other'),
    ], string='Verify type')
    punch_time = fields.Datetime(string='Punching Time')
    bio_id = fields.Char()
    terminal_sn = fields.Char()
    terminal_alias = fields.Char()
    sync = fields.Boolean(
        string='Sync',
        required=False)

    # @api.model
    # def process_attendance_from_biotime(self):
    #     today = fields.Date.today()
    #     tomorrow = today + timedelta(days=1)
    #
    #     # Step 1: Fetch today's unsynced transactions
    #     transactions = self.search([
    #         ('punch_time', '>=', fields.Datetime.to_string(today)),
    #         ('punch_time', '<', fields.Datetime.to_string(tomorrow)),
    #         ('sync', '=', False),
    #         ('employee_id','!=',False)
    #     ])
    #
    #     # Step 2: Group punches by employee and day
    #     emp_day_map = defaultdict(list)
    #     for tx in transactions:
    #         key = (tx.employee_id.id, tx.punch_time.date())
    #         emp_day_map[key].append(tx)
    #
    #     hr_attendance = self.env['hr.attendance']
    #
    #     for (emp_id, day), txs in emp_day_map.items():
    #         txs.sort(key=lambda t: t.punch_time)
    #         check_in = txs[0].punch_time
    #         check_out = txs[-1].punch_time
    #
    #         # ✅ Fix: If only one punch, use same for in/out
    #         if len(txs) == 1:
    #             check_out = check_in
    #
    #         # Search for existing attendance
    #         attendance = hr_attendance.search([
    #             ('employee_id', '=', emp_id),
    #             ('check_in', '>=', datetime.combine(day, datetime.min.time())),
    #             ('check_in', '<=', datetime.combine(day, datetime.max.time())),
    #         ], limit=1)
    #
    #         if attendance:
    #             updated = False
    #             if check_in < attendance.check_in:
    #                 attendance.check_in = check_in
    #                 updated = True
    #             if check_out > attendance.check_out:
    #                 attendance.check_out = check_out
    #                 updated = True
    #             if updated:
    #                 attendance.write({})
    #         else:
    #             hr_attendance.create({
    #                 'employee_id': emp_id,
    #                 'check_in': check_in,
    #                 'check_out': check_out,
    #             })
    #
    #         # Mark used punches as synced
    #         txs_to_sync = self.browse([t.id for t in txs])
    #         txs_to_sync.write({'sync': True})

    @api.model
    def process_attendance_from_biotime(self):
        # Step 1: Fetch all unsynced transactions
        transactions = self.search([('sync', '=', False),('employee_id','!=',False)])

        # Step 2: Group punches by employee and punch date
        emp_day_map = defaultdict(list)
        for tx in transactions:
            key = (tx.employee_id.id, tx.punch_time.date())
            emp_day_map[key].append(tx)

        hr_attendance = self.env['hr.attendance'].with_company(self.env.company.id)

        for (emp_id, punch_date), txs in emp_day_map.items():
            txs.sort(key=lambda t: t.punch_time)
            check_in = txs[0].punch_time
            check_out = txs[-1].punch_time

            attendance = hr_attendance.search([
                ('employee_id', '=', emp_id),
                ('check_in', '>=', datetime.combine(punch_date, datetime.min.time())),
                ('check_in', '<=', datetime.combine(punch_date, datetime.max.time())),
            ], limit=1)

            if attendance:
                updated = False

                if check_in < attendance.check_in:
                    # Shift old check_in to check_out if it's later than current check_out
                    if not attendance.check_out or attendance.check_in > attendance.check_out:
                        attendance.check_out = attendance.check_in
                    attendance.check_in = check_in
                    updated = True

                if check_out > attendance.check_in and (not attendance.check_out or check_out > attendance.check_out):
                    attendance.check_out = check_out
                    updated = True

                if updated:
                    attendance.write({})
            else:
                # One punch = check_in only
                if len(txs) == 1:
                    hr_attendance.create({
                        'employee_id': emp_id,
                        'check_in': check_in,
                        'company_id':self.env.company.id
                    })
                else:
                    hr_attendance.create({
                        'employee_id': emp_id,
                        'check_in': check_in,
                        'check_out': check_out,
                        'company_id': self.env.company.id
                    })

            # Mark transactions as synced
            self.browse([t.id for t in txs]).write({'sync': True})







    def generate_attendance(self):
        for rec in self.filtered(lambda l:not l.sync):
            if rec.employee_id :
                check_last_attendance = self.env['hr.attendance'].sudo().search([
                    ('employee_id', '=', rec.employee_id.id),
                    ('check_date', '=', rec.punch_time),
                ], limit=1)
                if check_last_attendance:
                    if check_last_attendance.check_in > rec.punch_time:
                        check_last_attendance.write({
                            'check_in': rec.punch_time
                        })
                    if (
                            check_last_attendance.check_out and rec.punch_time > check_last_attendance.check_out) or not check_last_attendance.check_out:
                        check_last_attendance.write({
                            'check_out': rec.punch_time
                        })

                else:
                    no_check_out_attendances = self.env['hr.attendance'].search([
                        ('employee_id', '=', rec.employee_id.id),
                        ('check_out', '=', False),
                    ], order='check_in desc')
                    if no_check_out_attendances:
                        for a in no_check_out_attendances:
                            a.check_out = a.check_in
                    self.env['hr.attendance'].sudo().create({
                        'employee_id': rec.employee_id.id,
                        'check_in': rec.punch_time
                    })
                rec.write({'sync':True})


class Attendance(models.Model):
    _inherit = 'hr.attendance'

    @api.constrains('check_in', 'check_out', 'employee_id')
    def _check_validity(self):
        pass
