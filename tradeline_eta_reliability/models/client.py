"""Bounded ETA transport. Never logs tokens, signing PINs, or invoice bodies."""
import time
import hashlib
from datetime import datetime

import requests

_TOKENS = {}
_REJECTED_TOKENS = set()


class EtaError(Exception):
    def __init__(self, message, status=0, retry_after=60):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class EtaClient:
    def __init__(self, company):
        if company.config_type not in ('prod', 'uat'):
            raise EtaError('Select an ETA environment on the company.')
        self.company = company
        self.session = requests.Session()
        self.token = None
        self.token_until = 0
        self.last_get = 0
        self.api = ('https://api.invoicing.eta.gov.eg' if company.config_type == 'prod'
                    else 'https://api.preprod.invoicing.eta.gov.eg')
        self.identity = ('https://id.eta.gov.eg' if company.config_type == 'prod'
                         else 'https://id.preprod.eta.gov.eg')

    def _authorize(self):
        if self.token and time.monotonic() < self.token_until:
            return
        if not self.company.client_id or not self.company.client_secret:
            raise EtaError('Configure ETA credentials on the invoice company.')
        key = (self.identity, self.company.client_id,
               hashlib.sha256(self.company.client_secret.encode()).hexdigest())
        cached = _TOKENS.get(key)
        if cached and cached[1] > time.monotonic():
            self.token, self.token_until = cached
            return
        if (self.company.generated_token and self.company.last_generated_token
                and hashlib.sha256(self.company.generated_token.encode()).hexdigest() not in _REJECTED_TOKENS
                and 0 <= (datetime.utcnow() - self.company.last_generated_token).total_seconds() < 3000):
            self.token = self.company.generated_token
            self.token_until = time.monotonic() + 60
            _TOKENS[key] = (self.token, self.token_until)
            return
        result = self.session.post(self.identity + '/connect/token', data={
            'grant_type': 'client_credentials', 'scope': 'InvoicingAPI',
            'client_id': self.company.client_id, 'client_secret': self.company.client_secret,
        }, timeout=(5, 25))
        if not result.ok:
            raise EtaError('ETA authentication failed (HTTP %s).' % result.status_code, result.status_code)
        body = result.json()
        if not body.get('access_token'):
            raise EtaError('ETA authentication returned no access token.')
        self.token = body['access_token']
        self.token_until = time.monotonic() + max(1, int(body.get('expires_in', 3600)) - 120)
        _TOKENS[key] = (self.token, self.token_until)

    def request(self, method, path, **kwargs):
        self._authorize()
        if method == 'GET':
            gap = 2.1 if path.endswith('/search') else 0.6
            time.sleep(max(0, gap - (time.monotonic() - self.last_get)))
            self.last_get = time.monotonic()
        response = self.session.request(method, self.api + path, headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json',
            'Content-Type': 'application/json',
        }, timeout=(5, 25), **kwargs)
        if not response.ok:
            try:
                retry = max(60, min(3600, int(response.headers.get('Retry-After', 60)) + 1))
            except (TypeError, ValueError):
                retry = 60
            if response.status_code == 401:
                _REJECTED_TOKENS.add(hashlib.sha256(self.token.encode()).hexdigest())
                self.token = None
                self.token_until = 0
                _TOKENS.clear()
            raise EtaError('ETA returned HTTP %s.' % response.status_code, response.status_code, retry)
        try:
            body = response.json()
        except ValueError as exc:
            raise EtaError('ETA returned an unreadable response.') from exc
        if not isinstance(body, dict):
            raise EtaError('ETA returned an unexpected response.')
        return body

    def close(self):
        self.session.close()
