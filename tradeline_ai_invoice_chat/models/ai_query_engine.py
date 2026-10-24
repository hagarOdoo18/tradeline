# -*- coding: utf-8 -*-
"""
Generic AI Data Query Engine
============================
Executes a *validated* query specification against any allowed model, using
the ORM with the **current user's** access rights (ACL, record rules,
multi-company and branch restrictions all apply).

Query specification (produced by the AI, never executed blindly):

    {
        "model":      "sale.order",
        "domain":     [["state", "=", "sale"],
                       ["date_order", ">=", "2025-01-01"]],
        "fields":     ["name", "partner_id", "amount_total"],   # list mode
        "group_by":   ["partner_id", "date_order:month"],       # group mode
        "aggregates": ["amount_total:sum", "__count"],
        "order":      "amount_total desc",
        "limit":      30
    }

No raw SQL, no arbitrary Python: only search_read / read_group.
"""
import logging
from datetime import date, datetime

from odoo import models, api
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

ALLOWED_OPERATORS = {
    '=', '!=', '>', '>=', '<', '<=',
    'like', 'not like', 'ilike', 'not ilike', '=like', '=ilike',
    'in', 'not in', 'child_of', 'parent_of', 'any', 'not any',
}
ALLOWED_AGGREGATES = ('sum', 'avg', 'min', 'max', 'count', 'count_distinct')
ALLOWED_GRANULARITY = ('day', 'week', 'month', 'quarter', 'year')

NUMERIC_TYPES = ('integer', 'float', 'monetary')
DEFAULT_LIMIT = 50
HARD_MAX_LIMIT = 500


def _fmt(value):
    """Render a single ORM value for the plain-text table."""
    if value is False or value is None:
        return '-'
    if value is True:
        return 'yes'
    if isinstance(value, (list, tuple)):
        # many2one -> (id, name) ; x2many -> list of ids
        if len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str):
            return value[1]
        return ', '.join(str(v) for v in value[:5])
    if isinstance(value, float):
        return '{:,.2f}'.format(value)
    if isinstance(value, int):
        return '{:,}'.format(value)
    if isinstance(value, (date, datetime)):
        return str(value)
    text = str(value).replace('\n', ' ').strip()
    return text[:60]


def _table(headers, rows, title=''):
    """Render a fixed-width text table the AI (and the fallback UI) can read."""
    if not rows:
        return (title + '\nNo records matched.') if title else 'No records matched.'
    widths = []
    for idx, head in enumerate(headers):
        width = len(str(head))
        for row in rows:
            width = max(width, len(str(row[idx])))
        widths.append(min(width, 42))

    def line(cells):
        out = []
        for idx, cell in enumerate(cells):
            text = str(cell)
            if len(text) > widths[idx]:
                text = text[:widths[idx] - 1] + '.'
            out.append(text.ljust(widths[idx]))
        return ' | '.join(out).rstrip()

    parts = []
    if title:
        parts.append(title)
    parts.append(line(headers))
    parts.append('-' * min(len(line(headers)), 160))
    parts.extend(line(r) for r in rows)
    return '\n'.join(parts)


class AiDataQueryEngine(models.AbstractModel):
    _name = 'ai.data.query.engine'
    _description = 'AI Generic Data Query Engine'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _max_rows(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'ai_invoice_chat.max_rows', '100')
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 100
        return max(1, min(value, HARD_MAX_LIMIT))

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def _base_field(self, spec_name):
        """'invoice_date:month' -> 'invoice_date' ; 'partner_id.country_id' -> 'partner_id'."""
        return str(spec_name or '').split(':')[0].split('.')[0].strip()

    def _check_field(self, fname, fields_meta, model_name, purpose='read'):
        base = self._base_field(fname)
        if not base:
            raise UserError("Empty field name in %s." % purpose)
        if base not in fields_meta:
            raise UserError(
                "Field '%s' does not exist (or is not readable) on %s. "
                "Call describe_model first." % (base, model_name))
        return base

    def _validate_domain(self, domain, fields_meta, model_name):
        if not domain:
            return []
        if not isinstance(domain, (list, tuple)):
            raise UserError('domain must be a list.')
        clean = []
        for item in domain:
            if isinstance(item, str):
                if item not in ('&', '|', '!'):
                    raise UserError("Invalid domain operator '%s'." % item)
                clean.append(item)
                continue
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise UserError('Each domain leaf must be [field, operator, value].')
            field, operator, value = item
            operator = str(operator).strip().lower()
            if operator not in ALLOWED_OPERATORS:
                raise UserError("Operator '%s' is not allowed." % operator)
            self._check_field(field, fields_meta, model_name, purpose='domain')
            if isinstance(value, (list, tuple)) and len(value) > 200:
                raise UserError('Domain value list is too long (max 200).')
            clean.append((str(field), operator, value))
        return clean

    def _validate_group_by(self, group_by, fields_meta, model_name):
        clean = []
        for entry in group_by or []:
            entry = str(entry).strip()
            base = self._check_field(entry, fields_meta, model_name, purpose='group_by')
            if ':' in entry:
                granularity = entry.split(':', 1)[1]
                if granularity not in ALLOWED_GRANULARITY:
                    raise UserError(
                        "Unknown date granularity '%s' (use %s)."
                        % (granularity, ', '.join(ALLOWED_GRANULARITY)))
                clean.append('%s:%s' % (base, granularity))
            else:
                clean.append(base)
        return clean

    def _validate_aggregates(self, aggregates, fields_meta, model_name):
        clean = []
        for entry in aggregates or []:
            entry = str(entry).strip()
            if entry in ('__count', 'count', '__count:sum'):
                continue                                   # always added
            if ':' not in entry:
                base = self._check_field(entry, fields_meta, model_name,
                                         purpose='aggregates')
                entry = '%s:sum' % base
            base, func = entry.split(':', 1)
            base = self._check_field(base, fields_meta, model_name,
                                     purpose='aggregates')
            func = func.strip().lower()
            if func not in ALLOWED_AGGREGATES:
                raise UserError(
                    "Aggregate '%s' is not allowed (use %s)."
                    % (func, ', '.join(ALLOWED_AGGREGATES)))
            if func in ('sum', 'avg') and \
                    fields_meta[base].get('type') not in NUMERIC_TYPES:
                raise UserError(
                    "Field '%s' is not numeric, cannot apply %s." % (base, func))
            clean.append('%s:%s' % (base, func))
        return clean

    def _default_fields(self, fields_meta):
        """Sensible column set when the AI does not specify fields."""
        preferred = ['display_name', 'name', 'partner_id', 'date', 'date_order',
                     'invoice_date', 'state', 'amount_total', 'amount_untaxed',
                     'product_id', 'quantity', 'product_uom_qty', 'price_unit',
                     'price_subtotal']
        chosen = [f for f in preferred if f in fields_meta]
        return chosen[:8] or ['display_name']

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run_query(self, spec):
        """Execute a validated query spec. Always returns a plain-text result."""
        spec = spec or {}
        Schema = self.env['ai.data.schema']
        model_name = (spec.get('model') or '').strip()

        allowed, message = Schema.is_model_allowed(model_name)
        if not allowed:
            return ('CANNOT READ THIS: %s\n(Tell the user in friendly plain words and offer something you can do instead.)' % message)

        fields_meta = Schema.get_readable_fields(model_name)
        if not fields_meta:
            return ("CANNOT READ THIS: there are no readable fields on '%s' for you."
                    % model_name)

        try:
            domain = self._validate_domain(spec.get('domain'), fields_meta, model_name)
            group_by = self._validate_group_by(spec.get('group_by'), fields_meta, model_name)
            aggregates = self._validate_aggregates(spec.get('aggregates'),
                                                   fields_meta, model_name)
        except UserError as err:
            return ('INVALID QUERY (fix it and retry, do not show this to the user): %s' % (err.args[0] if err.args else err))

        try:
            limit = int(spec.get('limit') or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, self._max_rows()))
        order = (spec.get('order') or '').strip() or False

        Model = self.env[model_name]
        try:
            if group_by:
                return self._run_grouped(Model, model_name, domain, group_by,
                                         aggregates, order, limit, fields_meta)
            return self._run_list(Model, model_name, domain, spec.get('fields'),
                                  order, limit, fields_meta)
        except AccessError as err:
            return ('NO ACCESS: %s\n(Tell the user their Odoo access rights do not cover this data, kindly.)' % (err.args[0] if err.args else err))
        except UserError as err:
            return 'QUERY REJECTED: %s' % (err.args[0] if err.args else err)
        except Exception as exc:                            # pragma: no cover
            _logger.warning('AI query failed on %s: %s', model_name, exc)
            return ('QUERY ERROR (do not show this text to the user): %s' % exc)

    # ------------------------------------------------------------------
    # Execution modes
    # ------------------------------------------------------------------
    def _run_list(self, Model, model_name, domain, fields, order, limit, fields_meta):
        if fields:
            columns = []
            for fname in fields:
                base = self._check_field(fname, fields_meta, model_name)
                if base not in columns:
                    columns.append(base)
            columns = columns[:12]
        else:
            columns = self._default_fields(fields_meta)

        total = Model.search_count(domain)
        records = Model.search_read(domain, columns, limit=limit, order=order or None)

        rows = [[_fmt(rec.get(col)) for col in columns] for rec in records]
        headers = [fields_meta[col].get('string') or col for col in columns]
        title = '=== %s -- %s record(s) matched, showing %s ===' % (
            model_name, '{:,}'.format(total), len(records))
        text = _table(headers, rows, title)

        numeric = [c for c in columns if fields_meta[c].get('type') in NUMERIC_TYPES]
        if numeric and records:
            sums = []
            for col in numeric:
                value = sum(float(r.get(col) or 0) for r in records)
                sums.append('%s=%s' % (col, '{:,.2f}'.format(value)))
            text += '\nSum of displayed rows: ' + ', '.join(sums)
        if total > len(records):
            text += ('\nNote: %s more record(s) not shown -- narrow the domain or '
                     'use group_by for totals.' % '{:,}'.format(total - len(records)))
        return text

    def _run_grouped(self, Model, model_name, domain, group_by, aggregates,
                     order, limit, fields_meta):
        measure_fields = list(aggregates)
        groups = None
        try:
            groups = Model.read_group(
                domain, measure_fields, group_by,
                limit=limit, orderby=order or False, lazy=False)
        except Exception as exc:
            _logger.info('read_group unavailable/failed (%s), trying _read_group', exc)
            groups = None

        headers = []
        rows = []

        if groups is not None:
            for spec_name in group_by:
                base = spec_name.split(':')[0]
                headers.append(fields_meta[base].get('string') or base)
            for agg in aggregates:
                headers.append(agg)
            headers.append('Count')

            for group in groups:
                row = []
                for spec_name in group_by:
                    key = spec_name if spec_name in group else spec_name.split(':')[0]
                    row.append(_fmt(group.get(key)))
                for agg in aggregates:
                    base = agg.split(':')[0]
                    row.append(_fmt(group.get(base)))
                row.append(_fmt(group.get('__count')))
                rows.append(row)
        else:
            # Odoo 18+ private API fallback
            agg_specs = [a.replace(':', ':') for a in aggregates] + ['__count']
            groups = Model._read_group(
                domain, groupby=group_by, aggregates=agg_specs,
                order=order or None, limit=limit)
            for spec_name in group_by:
                base = spec_name.split(':')[0]
                headers.append(fields_meta[base].get('string') or base)
            headers += list(aggregates) + ['Count']
            for tup in groups:
                rows.append([_fmt(value) for value in tup])

        title = '=== %s grouped by %s ===' % (model_name, ', '.join(group_by))
        text = _table(headers, rows, title)

        # Grand totals for numeric aggregate columns
        if rows:
            totals = []
            offset = len(group_by)
            for idx, agg in enumerate(aggregates):
                column = offset + idx
                total = 0.0
                ok = True
                for row in rows:
                    raw = str(row[column]).replace(',', '')
                    try:
                        total += float(raw)
                    except ValueError:
                        ok = False
                        break
                if ok:
                    totals.append('%s=%s' % (agg, '{:,.2f}'.format(total)))
            count_total = 0
            for row in rows:
                try:
                    count_total += int(str(row[-1]).replace(',', ''))
                except ValueError:
                    pass
            totals.append('records=%s' % '{:,}'.format(count_total))
            text += '\nTotals over shown groups: ' + ', '.join(totals)
        return text

    # ------------------------------------------------------------------
    # Convenience wrapper used by the keyword (no-AI) fallback
    # ------------------------------------------------------------------
    def quick_group(self, model_name, domain, group_by, aggregates, order=None,
                    limit=20):
        return self.run_query({
            'model': model_name,
            'domain': domain,
            'group_by': group_by,
            'aggregates': aggregates,
            'order': order,
            'limit': limit,
        })

    # ------------------------------------------------------------------
    # NEW TOOLS: get_field_values, count_records, search_by_name
    # ------------------------------------------------------------------
    def get_field_values(self, model_name, field_name, domain=None, limit=50):
        """Return distinct values of *field_name* on *model_name*.

        Uses read_group to get unique values efficiently. Returns a
        plain-text list the AI can use to build precise domain filters.
        """
        Schema = self.env['ai.data.schema']
        model_name = (model_name or '').strip()
        field_name = (field_name or '').strip()

        allowed, message = Schema.is_model_allowed(model_name)
        if not allowed:
            return 'CANNOT READ THIS: %s' % message

        fields_meta = Schema.get_readable_fields(model_name)
        if not fields_meta:
            return "No readable fields on '%s'." % model_name

        base = self._base_field(field_name)
        if base not in fields_meta:
            return ("Field '%s' does not exist on %s. "
                    "Call describe_model first." % (field_name, model_name))

        fdef = fields_meta[base]
        ftype = fdef.get('type')
        limit = max(1, min(int(limit or 50), 200))

        # For selection fields, return the defined options directly
        if ftype == 'selection':
            sel = fdef.get('selection') or []
            lines = ['=== Possible values of %s.%s (selection) ===' % (model_name, base)]
            for item in sel:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    lines.append('  %s = %s' % (item[0], item[1]))
            return '\n'.join(lines) if len(lines) > 1 else 'No selection values defined.'

        # For other fields, use read_group to get distinct values
        Model = self.env[model_name]
        clean_domain = []
        if domain:
            try:
                clean_domain = self._validate_domain(domain, fields_meta, model_name)
            except Exception:
                clean_domain = []

        # Add filter to exclude empty values
        if ftype in ('char', 'text', 'html'):
            clean_domain = clean_domain + [(base, '!=', False), (base, '!=', '')]
        elif ftype != 'boolean':
            clean_domain = clean_domain + [(base, '!=', False)]

        try:
            groups = Model.read_group(
                clean_domain, [base], [base],
                limit=limit, orderby='%s_count desc' % base if ftype not in ('date', 'datetime') else base,
                lazy=False,
            )
        except Exception:
            # Fallback: try _read_group (Odoo 18)
            try:
                groups = Model._read_group(
                    clean_domain, groupby=[base], aggregates=['__count'],
                    limit=limit)
                lines = ['=== Distinct values of %s.%s (%d shown) ===' % (
                    model_name, base, len(groups))]
                for tup in groups:
                    val = tup[0]
                    count = tup[1] if len(tup) > 1 else ''
                    lines.append('  %s  (%s records)' % (_fmt(val), _fmt(count)))
                return '\n'.join(lines)
            except Exception as exc:
                return 'Could not retrieve values: %s' % exc

        lines = ['=== Distinct values of %s.%s (%d shown) ===' % (
            model_name, base, len(groups))]
        for group in groups:
            val = group.get(base)
            count = group.get('%s_count' % base, group.get('__count', ''))
            lines.append('  %s  (%s records)' % (_fmt(val), _fmt(count)))
        if not groups:
            lines.append('  (no values found)')
        return '\n'.join(lines)

    def count_records(self, model_name, domain=None):
        """Fast record count without fetching data."""
        Schema = self.env['ai.data.schema']
        model_name = (model_name or '').strip()

        allowed, message = Schema.is_model_allowed(model_name)
        if not allowed:
            return 'CANNOT READ THIS: %s' % message

        fields_meta = Schema.get_readable_fields(model_name)
        clean_domain = []
        if domain:
            try:
                clean_domain = self._validate_domain(domain, fields_meta, model_name)
            except Exception as exc:
                return 'INVALID DOMAIN: %s' % exc

        try:
            total = self.env[model_name].search_count(clean_domain)
        except AccessError as err:
            return 'NO ACCESS: %s' % (err.args[0] if err.args else err)
        except Exception as exc:
            return 'COUNT ERROR: %s' % exc

        return '%s has %s record(s) matching the given domain.' % (
            model_name, '{:,}'.format(total))

    def search_by_name(self, model_name, name, limit=10):
        """Find records by name / reference / display_name.

        Returns a compact table with id, display_name, and a few
        key fields so the AI can identify the right record.
        """
        Schema = self.env['ai.data.schema']
        model_name = (model_name or '').strip()
        name = (name or '').strip()

        if not name:
            return 'Please provide a name or reference to search for.'

        allowed, message = Schema.is_model_allowed(model_name)
        if not allowed:
            return 'CANNOT READ THIS: %s' % message

        fields_meta = Schema.get_readable_fields(model_name)
        if not fields_meta:
            return "No readable fields on '%s'." % model_name

        limit = max(1, min(int(limit or 10), 50))
        Model = self.env[model_name]

        # Build an OR domain searching across likely name fields
        search_fields = []
        for fname in ('name', 'display_name', 'ref', 'default_code',
                      'x_name', 'x_studio_name', 'complete_name',
                      'partner_name', 'number'):
            if fname in fields_meta:
                search_fields.append(fname)

        if not search_fields:
            # Last resort: use name_search
            try:
                results = Model.name_search(name, limit=limit)
                if not results:
                    return "No records in %s matching '%s'." % (model_name, name)
                lines = ['=== Search results for "%s" in %s ===' % (name, model_name)]
                for rec_id, rec_name in results:
                    lines.append('  [%d] %s' % (rec_id, rec_name))
                return '\n'.join(lines)
            except Exception as exc:
                return 'SEARCH ERROR: %s' % exc

        # Build OR domain
        domain_parts = []
        for fname in search_fields:
            domain_parts.append((fname, 'ilike', name))
        if len(domain_parts) > 1:
            domain = ['|'] * (len(domain_parts) - 1) + domain_parts
        else:
            domain = domain_parts

        # Pick useful display columns
        display_cols = ['display_name']
        extras = ['state', 'partner_id', 'date', 'date_order', 'invoice_date',
                  'amount_total', 'amount_untaxed', 'product_id', 'quantity',
                  'categ_id', 'company_id', 'branch_id', 'email', 'phone',
                  'default_code', 'list_price', 'standard_price']
        for col in extras:
            if col in fields_meta and col not in display_cols:
                display_cols.append(col)
            if len(display_cols) >= 8:
                break

        try:
            records = Model.search_read(domain, display_cols, limit=limit)
        except AccessError as err:
            return 'NO ACCESS: %s' % (err.args[0] if err.args else err)
        except Exception as exc:
            return 'SEARCH ERROR: %s' % exc

        if not records:
            return "No records in %s matching '%s'." % (model_name, name)

        # Format as table
        cols_with_id = ['id'] + display_cols
        headers = ['ID'] + [fields_meta.get(c, {}).get('string', c) for c in display_cols]
        rows = []
        for rec in records:
            row = [str(rec.get('id', ''))]
            for col in display_cols:
                row.append(_fmt(rec.get(col)))
            rows.append(row)

        title = '=== Search results for "%s" in %s (%d found) ===' % (
            name, model_name, len(records))
        return _table(headers, rows, title)
