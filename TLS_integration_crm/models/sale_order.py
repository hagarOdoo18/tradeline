from odoo import fields, models, api

from odoo.fields import datetime
from datetime import timedelta
import json
import logging
import requests
import time
from dateutil.relativedelta import relativedelta


_logger = logging.getLogger(__name__)


class sale_order(models.Model):
    _inherit = 'sale.order'
    is_tvc = fields.Boolean(
         string='Is_tvc',
         required=False)

    is_point = fields.Boolean (
        string='Is_point',
        required=False)