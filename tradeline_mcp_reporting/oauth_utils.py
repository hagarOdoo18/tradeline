"""Small OAuth primitives; no network calls or credential logging."""
import base64
import hashlib
import re
import secrets
from urllib.parse import urlsplit

SCOPE = "odoo.reporting.read"
# Exact callback documented by Anthropic; do not trust a client-supplied name.
CLAUDE_CALLBACKS = frozenset({"https://claude.ai/api/mcp/auth_callback"})


def opaque():
    return secrets.token_urlsafe(48)


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def challenge(verifier):
    if not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        raise ValueError("Invalid PKCE verifier.")
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


def canonical_url(value, local_test=False):
    parsed = urlsplit(value)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not parsed.hostname or (parsed.scheme != "https" and not
                (local_test and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}))):
        raise ValueError("Hosted MCP requires the canonical HTTPS Odoo URL.")
    return value.rstrip("/")


def registration(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid client metadata.")
    redirects = data.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or len(redirects) > 3 or any(u not in CLAUDE_CALLBACKS for u in redirects):
        raise ValueError("Only Claude's registered HTTPS callback is supported.")
    if data.get("token_endpoint_auth_method", "none") != "none":
        raise ValueError("Use a public OAuth client with PKCE (Register automatically in Claude).")
    if set(data.get("grant_types", ["authorization_code", "refresh_token"])) - {"authorization_code", "refresh_token"}:
        raise ValueError("Unsupported grant type.")
    if data.get("response_types", ["code"]) != ["code"]:
        raise ValueError("Only authorization code responses are supported.")
    name = data.get("client_name", "Claude")
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise ValueError("Invalid client name.")
    # Only retain the metadata we use. No supplied URLs are fetched.
    return {"client_name": name, "redirect_uris": sorted(set(redirects))}
