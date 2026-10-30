"""From this host, outside the catalog's VPC, the private API cannot be reached (#13).

usage: outside.py <api-url> <api-endpoint-url> <warehouse>

For each URL (the private-DNS name, and the VPC endpoint's own name): whether its host resolves here,
to what kind of address, and what an anonymous GET of /v1/config gets (10 s at most). Prints one line
per URL with names and address kinds, never the addresses. Exits 1 if any request was served (2xx), or
if a URL is not https.
"""
import ipaddress
import socket
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


def check(url, warehouse):
    if urlparse(url).scheme != "https":
        raise SystemExit(f"{name(url)}: not an https URL")
    host = urlparse(url).hostname
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except socket.gaierror:
        return f"{name(url)}: does not resolve outside the VPC", False
    kinds = sorted({"private" if ipaddress.ip_address(a).is_private else "public" for a in addresses})
    request = urllib.request.Request(url.rstrip("/") + "/v1/config?warehouse=" + warehouse)
    try:
        # https only (checked above), so no file: or custom scheme reaches urlopen.
        with urllib.request.urlopen(request, timeout=10, context=ssl.create_default_context()) as response:  # nosec B310
            answer, served = f"HTTP {response.status}", 200 <= response.status < 300
    except urllib.error.HTTPError as e:
        answer, served = f"HTTP {e.code}", False
    except (OSError, ssl.SSLError) as e:
        answer, served = f"no answer ({type(e).__name__})", False
    return f"{name(url)}: resolves to {len(addresses)} {'/'.join(kinds)} address(es); anonymous GET: {answer}", served


def name(url):
    """The URL's kind, not its host: placeholders only in evidence that may be quoted."""
    host = urlparse(url).hostname or ""
    return "VPC endpoint name (<api>-<vpce>)" if "-vpce-" in host else "private DNS name (<api>)"


def main():
    api_url, endpoint_url, warehouse = sys.argv[1:4]
    served = False
    for url in (api_url, endpoint_url):
        line, ok = check(url, warehouse)
        served |= ok
        print(line)
    return 1 if served else 0


if __name__ == "__main__":
    sys.exit(main())
