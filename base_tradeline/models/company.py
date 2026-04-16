import logging
from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import parse_date, SQL

_logger = logging.getLogger(__name__)
try:
    from num2words import num2words
except ImportError:
    _logger.warning("The num2words python library is not installed, amount-to-text features won't be fully available.")
    num2words = None
class Currency(models.Model):
    _inherit = "res.currency"
    def en_amount_to_text(self, amount):
        self.ensure_one()

        def _num2words(number, lang):
            try:
                return num2words(number, lang=lang).title()
            except NotImplementedError:
                return num2words(number, lang='en').title()

        if num2words is None:
            logging.getLogger(__name__).warning("The library 'num2words' is missing, cannot render textual amounts.")
            return ""

        integral, _sep, fractional = f"{amount:.{self.decimal_places}f}".partition('.')
        integer_value = int(integral)
        lang = tools.get_lang(self.env)
        if self.is_zero(amount - integer_value):
            return _(
                '%(integral_amount)s %(currency_unit)s',
                integral_amount=_num2words(integer_value, lang=lang.iso_code),
                currency_unit=self.currency_unit_label,
            )
        else:
            return _(
                '%(integral_amount)s %(currency_unit)s و %(fractional_amount)s %(currency_subunit)s',
                integral_amount=_num2words(integer_value, lang=lang.iso_code),
                currency_unit=self.currency_unit_label,
                fractional_amount=_num2words(int(fractional or 0), lang=lang.iso_code),
                currency_subunit=self.currency_subunit_label,
            )


    def amount_to_text(self, amount):
        self.ensure_one()

        def _num2words(number, lang):
            try:
                return num2words(number, lang=lang).title()
            except NotImplementedError:
                return num2words(number, lang='en').title()

        if num2words is None:
            logging.getLogger(__name__).warning("The library 'num2words' is missing, cannot render textual amounts.")
            return ""

        integral, _sep, fractional = f"{amount:.{self.decimal_places}f}".partition('.')
        integer_value = int(integral)
        lang = tools.get_lang(self.env)
        currency_unit_label = "ﺟﻨﻴﻬﺎ ﻓﻘﻂ لا غير"
        if self.is_zero(amount - integer_value):
            return _(
                '%(integral_amount)s %(currency_unit)s',
                integral_amount=_num2words(integer_value, lang='ar'),
                currency_unit=currency_unit_label,
            )
        else:
            return _(
                '%(integral_amount)s %(currency_unit)s and %(fractional_amount)s %(currency_subunit)s',
                integral_amount=_num2words(integer_value, lang='ar'),
                currency_unit=self.currency_unit_label,
                fractional_amount=_num2words(int(fractional or 0), lang='ar'),
                currency_subunit=currency_unit_label,
            )


class Company(models.Model):
    _inherit = 'res.company'

    sale_note = fields.Html(string='Default Terms and Conditions', )
    sale_note_en = fields.Html(string='EN Default Terms and Conditions')
