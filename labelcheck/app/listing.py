"""E-commerce listing processing for compliance checking (Rule 6(10)).

Note: Automated scraping / fetching of third-party e-commerce marketplace URLs
is disabled as commercial platforms prohibit automated scraping in their terms
and employ bot-mitigation. E-commerce listing verification is performed directly
on text provided by the user.
"""
import ipaddress
import json
import os
import socket
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def allow_private():
    """Tests only: let webhooks reach localhost. Refused in production (config.problems)."""
    return os.environ.get("LABELCHECK_ALLOW_PRIVATE_FETCH") == "1"


class FetchError(Exception):
    pass


def check_url(url):
    """Public http(s) address on port 80/443 only (SSRF protection for webhooks). Raises FetchError."""
    return _check_host(url)


def _check_host(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise FetchError("Only http and https links are allowed.")
    if not u.hostname:
        raise FetchError("The link has no host name.")
    port = u.port or (443 if u.scheme == "https" else 80)
    if port not in (80, 443) and not allow_private():
        raise FetchError("Only standard web ports (80, 443) are allowed.")
    try:
        infos = socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchError(f"Could not find the site '{u.hostname}'.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global and not allow_private():
            raise FetchError("That address points to a private or internal network; not allowed.")


def _jsonld_products(soup):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                t = d.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    out.append(d)
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
            elif isinstance(d, list):
                stack.extend(d)
    return out


def html_to_text(html):
    """Structured product data first (JSON-LD), then the visible page text."""
    soup = BeautifulSoup(html, "lxml")
    lines = []
    for p in _jsonld_products(soup):
        if p.get("name"):
            lines.append(str(p["name"]))
        offers = p.get("offers")
        for o in (offers if isinstance(offers, list) else [offers] if offers else []):
            if isinstance(o, dict) and o.get("price"):
                lines.append(f"MRP Rs. {o['price']}" if o.get("priceCurrency", "INR") == "INR"
                             else f"Price {o.get('priceCurrency')} {o['price']}")
        for k in ("description", "countryOfOrigin", "manufacturer", "brand"):
            v = p.get(k)
            if isinstance(v, dict):
                v = v.get("name")
            if v:
                lines.append(f"{'Country of origin: ' if k == 'countryOfOrigin' else ''}{v}")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        t.decompose()
    for ln in soup.get_text("\n").splitlines():
        ln = " ".join(ln.split())
        if ln:
            lines.append(ln)
    text = "\n".join(lines)
    return text[:60000]
