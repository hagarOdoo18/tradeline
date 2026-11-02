"""Source-aware historical units through the existing approved read-only query API.

Original source product IDs are never treated as current catalogue IDs. Monthly
summary facts, sales orders, POS and current invoices are excluded from this report.
"""
import datetime
import re
from decimal import Decimal

class LegacyReportError(ValueError):pass

def namespace(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',value):
        raise LegacyReportError('Select the original source_db namespace from saved legacy records.')
    return value

def checked(result):
    if not isinstance(result,dict) or not isinstance(result.get('rows'),list):
        raise LegacyReportError('Historical query unavailable; unavailable is not zero.')
    if result.get('truncated') or result.get('cells_truncated'):
        raise LegacyReportError('Historical result is truncated. Narrow the dates or source products; do not report partial totals.')
    return result

def base(source):
    return [['source_db','=',namespace(source)],['state','in',['open','paid']],['invoice_type','in',['out_invoice','out_refund']]]

def search_legacy_products(call,search,source_db,limit=100):
    if not isinstance(search,str) or not 2<=len(search.strip())<=160:
        raise LegacyReportError('Use a product phrase of 2–160 characters.')
    if type(limit) is not int or not 1<=limit<=500:raise LegacyReportError('Limit must be 1–500.')
    result=call('query',{'spec':{'model':'legacy.invoice.line','domain':base(source_db)+[['name','ilike',search.strip()],['product_source_id','>',0]],'group_by':['product_source_id','name','source_uom_name'],'aggregates':['__count'],'include_archived':True,'limit':limit,'title':'Original legacy product candidates: '+search.strip()}})
    if not isinstance(result,dict):raise LegacyReportError('Historical discovery unavailable.')
    return {'source_db':source_db,'original_source_products':result,'selection_required':True,'selection_note':'Select source product IDs and inspect names to exclude accessories. These are original IDs, never current product IDs. Multiple names for one source ID are possible. Truncated discovery must be narrowed.','includes_unmapped_current_products':True}

def legacy_product_sales_units(call,spec):
    if not isinstance(spec,dict) or set(spec)-{'source_db','source_product_ids','date_from','date_to','grain','company_ids','title'}:
        raise LegacyReportError('Unsupported historical unit specification.')
    source=namespace(spec.get('source_db'));ids=spec.get('source_product_ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(type(v) is not int or v<=0 for v in ids):
        raise LegacyReportError('Select 1–500 positive original source product IDs.')
    ids=sorted(set(ids));domain=base(source)+[['product_source_id','in',ids]]
    start,end=spec.get('date_from'),spec.get('date_to')
    if (start is None)!=(end is None):raise LegacyReportError('Provide both date_from and date_to, or omit both for all saved history.')
    if start is not None:
        try:
            if not all(isinstance(v,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',v) for v in (start,end)):raise ValueError()
            a,b=map(datetime.date.fromisoformat,(start,end))
            if a>b:raise ValueError()
        except (ValueError,TypeError):raise LegacyReportError('Use ordered YYYY-MM-DD dates.') from None
        domain += [['invoice_date','>=',start],['invoice_date','<=',end]]
    companies=spec.get('company_ids')
    if companies is not None:
        if not isinstance(companies,list) or not 1<=len(companies)<=50 or any(type(v) is not int or v<=0 for v in companies):raise LegacyReportError('company_ids must contain positive current company IDs within your approved scope.')
        domain += [['company_id','in',sorted(set(companies))]]
    grain=spec.get('grain','total')
    if grain not in {'total','month'}:raise LegacyReportError('grain must be total or month.')
    title=spec.get('title','Historical net units by original source products')
    if not isinstance(title,str) or len(title)>200:raise LegacyReportError('Use a title of at most 200 characters.')
    groups=(['invoice_date:month'] if grain=='month' else [])+['uom_id','source_uom_name']
    evidence={};buckets={}
    for kind,label in [('out_invoice','gross_units'),('out_refund','credit_units')]:
        response=checked(call('query',{'spec':{'model':'legacy.invoice.line','domain':domain+[['invoice_type','=',kind]],'group_by':groups,'aggregates':['quantity:sum','__count'],'include_archived':True,'limit':500,'title':title+' — '+kind}}))
        evidence[kind]=response
        for row in response['rows']:
            unit=row.get('uom_id');unit_id=unit[0] if isinstance(unit,list) else unit
            original=row.get('source_uom_name') or None
            period=row.get('invoice_date:month') if grain=='month' else 'All selected saved dates'
            key=(period,unit_id,original)
            bucket=buckets.setdefault(key,{'period':period,'uom_id':unit,'source_uom_name':original,'gross_units':Decimal(0),'credit_units':Decimal(0),'invoice_lines':0,'credit_lines':0,'unit_verified':bool(unit_id and original)})
            amount=Decimal(str(row['quantity:sum']))
            if not amount.is_finite():raise LegacyReportError('Invalid saved quantity.')
            bucket[label]+=amount;bucket['invoice_lines' if kind=='out_invoice' else 'credit_lines']+=row['__count']
    rows=[]
    for bucket in buckets.values():
        bucket['net_units']=bucket['gross_units']-bucket['credit_units']
        for key in ('gross_units','credit_units','net_units'):bucket[key]=float(bucket[key])
        rows.append(bucket)
    return {'definition':'Posted legacy customer invoice quantities less legacy credit-note quantities, grouped by saved unit. Current product mappings are not required.','source_db':source,'source_product_ids':ids,'date_from':start,'date_to':end,'grain':grain,'rows':rows,'evidence':evidence,'truncated':False,'coverage_note':'Only saved legacy invoice lines visible within this user/company scope. Omitted dates mean available saved history, not certified lifetime history. Keep unit groups separate; false unit_verified means quantities cannot be certified as comparable units. Do not add monthly summaries or legacy sales orders to these invoice units. Current production sales remain a separate source.'}
