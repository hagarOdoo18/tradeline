# -*- coding: utf-8 -*-
"""
AI Data Schema Registry
=======================
Lets the assistant discover *what* it is allowed to read from the whole
Odoo database (not only invoices), while keeping technical and sensitive
data out of reach.

Two layers of protection:
  1. A static blacklist of technical / credential models and fields.
  2. Odoo's own ACL + record rules -- every check is done with the
     **current user's** rights, never sudo.
"""
import logging

from odoo import models, api

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard blacklist -- never exposed, whatever the settings say
# ---------------------------------------------------------------------------
BLOCKED_MODEL_PREFIXES = (
    'ir.', 'bus.', 'iap.', 'base_import.', 'web_editor.', 'web_tour.',
    'report.', 'auth_', 'mail.tracking', 'website.visitor', 'ai.chat.',
    'ai.data.', 'invoice.query.',
)
BLOCKED_MODELS = {
    'res.users.apikeys',
    'res.users.apikeys.description',
    'res.users.deletion',
    'res.users.settings',
    'res.users.log',
    'change.password.user',
    'change.password.wizard',
}
BLOCKED_FIELD_HINTS = (
    'password', 'passwd', 'api_key', 'apikey', 'secret', 'token',
    'private_key', 'signature', 'otp_', 'totp', 'access_token',
)
BLOCKED_FIELD_TYPES = ('binary',)
BLOCKED_FIELD_NAMES = {
    'password', 'password_crypt', 'new_password', 'sign_signature',
    'sign_initials', 'image_1920', 'image_1024', 'image_512',
    'image_256', 'image_128', 'avatar_1920', 'avatar_1024', 'avatar_512',
    'avatar_256', 'avatar_128',
}

# ---------------------------------------------------------------------------
# Curated business catalog -- the "map" handed to the AI so it knows where
# to look before it starts describing models.
# ---------------------------------------------------------------------------
DOMAIN_CATALOG = [
    ('Sales', [
        ('sale.order', 'Sales orders / quotations'),
        ('sale.order.line', 'Sales order lines (product, qty, price, discount)'),
        ('crm.team', 'Sales teams'),
        ('crm.lead', 'Leads / opportunities'),
    ]),
    ('Invoicing & Accounting', [
        ('account.move', 'Invoices, bills, credit notes and journal entries'),
        ('account.move.line', 'Journal items / invoice lines'),
        ('account.payment', 'Customer & vendor payments'),
        ('account.journal', 'Journals (bank, cash, sales, purchase)'),
        ('account.account', 'Chart of accounts'),
        ('account.tax', 'Taxes'),
        ('account.analytic.line', 'Analytic entries'),
        ('account.analytic.account', 'Analytic accounts'),
    ]),
    ('Purchase', [
        ('purchase.order', 'Purchase orders / RFQs'),
        ('purchase.order.line', 'Purchase order lines'),
    ]),
    ('Inventory', [
        ('stock.picking', 'Transfers (receipts, deliveries, internal)'),
        ('stock.move', 'Stock moves'),
        ('stock.move.line', 'Detailed operations (lot / serial level)'),
        ('stock.quant', 'On-hand quantities per location'),
        ('stock.location', 'Locations'),
        ('stock.warehouse', 'Warehouses'),
        ('stock.lot', 'Lots / serial numbers'),
        ('stock.valuation.layer', 'Stock valuation layers (cost)'),
    ]),
    ('Point of Sale', [
        ('pos.order', 'POS orders'),
        ('pos.order.line', 'POS order lines'),
        ('pos.payment', 'POS payments'),
        ('pos.payment.method', 'POS payment methods'),
        ('pos.session', 'POS sessions'),
        ('pos.config', 'POS terminals'),
    ]),
    ('Products', [
        ('product.template', 'Products (templates)'),
        ('product.product', 'Product variants'),
        ('product.category', 'Product categories'),
        ('product.pricelist', 'Pricelists'),
        ('product.pricelist.item', 'Pricelist rules'),
    ]),
    ('Contacts', [
        ('res.partner', 'Customers, vendors and contacts'),
    ]),
    ('Human Resources', [
        ('hr.employee', 'Employees'),
        ('hr.department', 'Departments'),
        ('hr.job', 'Job positions'),
        ('hr.contract', 'Employee contracts'),
        ('hr.attendance', 'Attendances'),
        ('hr.leave', 'Time off requests'),
        ('hr.leave.allocation', 'Time off allocations'),
        ('hr.expense', 'Expenses'),
    ]),
    ('Payroll', [
        ('hr.payslip', 'Payslips'),
        ('hr.payslip.line', 'Payslip lines (salary rules)'),
        ('hr.payslip.run', 'Payslip batches'),
        ('hr.salary.rule', 'Salary rules'),
    ]),
    ('Manufacturing', [
        ('mrp.production', 'Manufacturing orders'),
        ('mrp.bom', 'Bills of materials'),
    ]),
    ('Projects', [
        ('project.project', 'Projects'),
        ('project.task', 'Tasks'),
    ]),
    ('Organisation', [
        ('res.company', 'Companies'),
        ('res.branch', 'Branches'),
        ('res.users', 'Users'),
        ('res.currency', 'Currencies'),
    ]),
]


class AiDataSchema(models.AbstractModel):
    _name = 'ai.data.schema'
    _description = 'AI Data Schema Registry'

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------
    def _param(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(key, default)

    def _allow_all_models(self):
        return str(self._param('ai_invoice_chat.allow_all_models', 'False')).lower() \
            in ('1', 'true', 'yes')

    def _extra_models(self):
        raw = self._param('ai_invoice_chat.extra_models', '') or ''
        return {m.strip() for m in raw.replace('\n', ',').split(',') if m.strip()}

    def _catalog_models(self):
        names = set()
        for _domain, entries in DOMAIN_CATALOG:
            for model_name, _label in entries:
                names.add(model_name)
        names |= self._extra_models()
        return names

    # ------------------------------------------------------------------
    # Access checks
    # ------------------------------------------------------------------
    def _is_blacklisted(self, model_name):
        if not model_name or model_name in BLOCKED_MODELS:
            return True
        return any(model_name.startswith(p) for p in BLOCKED_MODEL_PREFIXES)

    def _user_can_read(self, model_name):
        """True when the *current* user may read this model (ACL level)."""
        if model_name not in self.env:
            return False
        Model = self.env[model_name]
        try:
            if hasattr(Model, 'has_access'):
                return bool(Model.has_access('read'))
            return bool(Model.check_access_rights('read', raise_exception=False))
        except Exception:                                   # pragma: no cover
            return False

    def is_model_allowed(self, model_name):
        """Full gate: blacklist + catalog/allow-all + user ACL."""
        if self._is_blacklisted(model_name):
            return False, "Model '%s' is not available to the assistant." % model_name
        if model_name not in self.env:
            return False, "Model '%s' does not exist in this database." % model_name
        if not self._allow_all_models() and model_name not in self._catalog_models():
            return False, (
                "Model '%s' is outside the allowed catalog. Ask an administrator to "
                "add it in Settings > AI Assistant > Additional Models, or use one of "
                "the catalog models." % model_name
            )
        if not self._user_can_read(model_name):
            return False, (
                "You do not have read access to '%s'." % model_name
            )
        return True, ''

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------
    def _is_field_allowed(self, fname, fdef):
        if fname in BLOCKED_FIELD_NAMES:
            return False
        if fdef.get('type') in BLOCKED_FIELD_TYPES:
            return False
        low = fname.lower()
        return not any(hint in low for hint in BLOCKED_FIELD_HINTS)

    def get_readable_fields(self, model_name):
        """Return {name: field_def} the user may read, minus blacklisted ones."""
        if model_name not in self.env:
            return {}
        try:
            fields_meta = self.env[model_name].fields_get()
        except Exception as exc:                            # pragma: no cover
            _logger.warning("fields_get failed on %s: %s", model_name, exc)
            return {}
        return {
            name: fdef for name, fdef in fields_meta.items()
            if self._is_field_allowed(name, fdef)
        }

    # ------------------------------------------------------------------
    # Text output for the AI prompt
    # ------------------------------------------------------------------
    def get_catalog_text(self):
        """Compact map of the business data the current user can reach."""
        lines = ['=== Available data domains (models you can query) ===']
        for domain, entries in DOMAIN_CATALOG:
            available = []
            for model_name, label in entries:
                if self._is_blacklisted(model_name):
                    continue
                if model_name not in self.env:
                    continue
                if not self._user_can_read(model_name):
                    continue
                available.append('%s (%s)' % (model_name, label))
            if available:
                lines.append('* %s: %s' % (domain, '; '.join(available)))
        extra = sorted(m for m in self._extra_models()
                       if m in self.env and self._user_can_read(m))
        if extra:
            lines.append('* Additional: %s' % ', '.join(extra))
        if self._allow_all_models():
            lines.append('* Any other model in the database may be queried too '
                         '(use search_models to find it).')
        return '\n'.join(lines)

    def search_models(self, keyword='', limit=30):
        """Find technical model names by keyword (label or technical name)."""
        keyword = (keyword or '').strip()
        IrModel = self.env['ir.model'].sudo()
        domain = []
        if keyword:
            domain = ['|', ('name', 'ilike', keyword), ('model', 'ilike', keyword)]
        records = IrModel.search(domain, limit=300, order='model')
        allowed_catalog = self._catalog_models()
        allow_all = self._allow_all_models()
        out = []
        for rec in records:
            if self._is_blacklisted(rec.model):
                continue
            if not allow_all and rec.model not in allowed_catalog:
                continue
            if not self._user_can_read(rec.model):
                continue
            out.append('%-38s %s' % (rec.model, rec.name))
            if len(out) >= limit:
                break
        if not out:
            return ("No queryable model matched '%s'. Use the catalog names listed in "
                    "the system context." % keyword)
        return '=== Models matching "%s" ===\n%s' % (keyword, '\n'.join(out))

    def describe_model(self, model_name, max_fields=140):
        """Field list of a model: name, label, type, relation, selection values."""
        allowed, msg = self.is_model_allowed(model_name)
        if not allowed:
            return msg
        fields_meta = self.get_readable_fields(model_name)
        if not fields_meta:
            return "No readable fields on '%s'." % model_name

        stored, computed = [], []
        for name, fdef in sorted(fields_meta.items()):
            ftype = fdef.get('type')
            label = fdef.get('string') or name
            extra = ''
            if ftype in ('many2one', 'one2many', 'many2many'):
                extra = ' -> %s' % fdef.get('relation', '?')
            elif ftype == 'selection':
                sel = fdef.get('selection') or []
                keys = [str(s[0]) for s in sel if isinstance(s, (list, tuple))][:12]
                if keys:
                    extra = ' [%s]' % ', '.join(keys)
            row = '  %-32s %-12s %s%s' % (name, ftype, label, extra)
            if fdef.get('store', True):
                stored.append(row)
            else:
                computed.append(row)

        lines = ['=== Fields of %s ===' % model_name,
                 '(only "stored" fields can be used in group_by / aggregates)',
                 '--- stored ---']
        lines += stored[:max_fields]
        if computed:
            lines.append('--- non-stored (readable, but not groupable) ---')
            lines += computed[:40]
        return '\n'.join(lines)
