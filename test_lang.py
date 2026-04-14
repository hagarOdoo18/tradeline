import xmlrpc.client
print('starting')
import os
url = 'https://tradelinestores-production-25284095.dev.odoo.com'
db = 'tradelinestores-production-25284095'
common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
uid = common.authenticate(db, 'admin', '3c333df7916eb9c75b83beb72e255d867e9ff1dd', {})
models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')
currencies = models.execute_kw(db, uid, '3c333df7916eb9c75b83beb72e255d867e9ff1dd', 'res.currency', 'search_read', [[('id', 'in', [76])], '|', ('active', '=', True), ('active', '=', False)], {'fields': ['id', 'name', 'active']})
print(currencies)
