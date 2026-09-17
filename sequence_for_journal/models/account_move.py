# -*- coding: utf-8 -*-
###############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Akhil Ashok(odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU AFFERO GENERAL PUBLIC LICENSE (AGPL v3) for more details.
#
#    You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
#    (AGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
from psycopg2 import errors as pgerrors

from odoo import models
from odoo.tools import SQL


class AccountMove(models.Model):
    """inherit account.move to add methods"""
    _inherit = 'account.move'

    def _get_starting_sequence(self):
        """Overriding the methode, this methode get the initial sequence of a
        journal"""
        self.ensure_one()
        # if self.journal_id.type in ['sale', 'bank', 'cash'] and \
        #         self.journal_id.sequence_id.suffix:
        #     starting_sequence = "%s/%s/%s%s" % (
        #         self.journal_id.sequence_id.prefix,
        #         self.date.year,
        #         self.journal_id.step_size,
        #         self.journal_id.sequence_id.suffix)
        if self.journal_id.type in ['sale', 'bank', 'cash']:
            starting_sequence = "%s/%s/000%d" % (
                self.journal_id.code,
                self.date.year,
               1)
        elif self.journal_id.type in ['purchase', 'general'] and \
                self.journal_id.sequence_id.suffix:
            starting_sequence = "%s/%s/%s%s" % (
                self.journal_id.sequence_id.prefix, self.date.year,
                self.journal_id.step_size,
                self.journal_id.sequence_id.suffix)
        else:
            starting_sequence = "%s/%s/%s000" % (
                self.journal_id.code, self.date.year,
                self.journal_id.default_step_size)
        if self.journal_id.refund_sequence and self.move_type in (
                'out_refund', 'in_refund'):
            starting_sequence = "RI" + starting_sequence

        return starting_sequence

    def _get_journal_sequence_step(self):
        """Return the configured increment while always making progress."""
        self.ensure_one()
        if (
            self.move_type == 'out_invoice'
            and self.journal_id.sequence_id
            and self.journal_id.sequence_id.number_increment > 0
        ):
            return self.journal_id.sequence_id.number_increment
        if (
            self.move_type == 'out_refund'
            and self.journal_id.re_sequence_id
            and self.journal_id.re_sequence_id.number_increment > 0
        ):
            return self.journal_id.re_sequence_id.number_increment
        return max(self.journal_id.default_step_size, 1)

    def _get_next_sequence_format(self):
        """Keep the configured journal format and let Odoo lock allocation."""
        format_string, format_values = super()._get_next_sequence_format()
        sequence = False
        if self.move_type == 'out_invoice':
            sequence = self.journal_id.sequence_id
        elif self.move_type == 'out_refund':
            sequence = self.journal_id.re_sequence_id

        if sequence:
            interpolated_prefix, interpolated_suffix = sequence._get_prefix_suffix()
            format_values['prefix1'] = (interpolated_prefix or '') + '/'
            format_values['suffix'] = (
                '/' + interpolated_suffix if sequence.suffix else ''
            )
        elif format_values.get('year_length'):
            format_values['year'] = self._truncate_year_to_length(
                self.date.year, format_values['year_length'])
        return format_string, format_values

    def _locked_increment(self, format_string, format_values):
        """Allocate the configured step under Odoo 18's unique-index lock.

        The previous implementation assigned a number only in the ORM cache.
        Concurrent POS invoices could therefore choose the same number and one
        transaction failed on ``account_move_unique_name``. This retains the
        custom increment while using the same database locking/retry strategy
        as Odoo's sequence mixin.
        """
        self.ensure_one()
        step = self._get_journal_sequence_step()
        cache = self._get_sequence_cache()
        seq = format_values.pop('seq')
        cache_key = (
            format_string.format(**format_values, seq=0),
            self._sequence_index and self[self._sequence_index],
            step,
        )
        if cache_key in cache:
            cache[cache_key] += step
            return format_string.format(**format_values, seq=cache[cache_key])

        self.flush_recordset()
        with self.env.cr.savepoint(flush=False) as savepoint:
            while True:
                seq += step
                sequence = format_string.format(**format_values, seq=seq)
                try:
                    self.env.cr.execute(
                        SQL(
                            "UPDATE %(table)s SET %(field)s = %(sequence)s "
                            "WHERE id = %(id)s",
                            table=SQL.identifier(self._table),
                            field=SQL.identifier(self._sequence_field),
                            sequence=sequence,
                            id=self.id,
                        ),
                        log_exceptions=False,
                    )
                    cache[cache_key] = seq
                    return sequence
                except (pgerrors.ExclusionViolation, pgerrors.UniqueViolation):
                    savepoint.rollback()
