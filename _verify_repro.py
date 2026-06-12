import sys
sys.path.insert(0, ".")
from web_agent.config import _normalize_domain_patterns
from web_agent.utils import _normalize_host, _matches_domain

cases = [
    ("evil.com:8443", "https://evil.com:8443/path"),
    ("[::1]", "http://[::1]/x"),
    ("http://[2001:db8::1]:9999/x", "http://[2001:db8::1]:9999/x"),
    ("localhost:8888", "http://localhost:8888/admin"),
    ("internal.svc:8080", "http://internal.svc:8080/api"),
    # control: a normal pattern that SHOULD work
    ("evil.com", "https://evil.com/path"),
    ("https://Evil.com/", "https://evil.com/path"),
    # IPv6 textual-equivalence cases (the bug under fix):
    # expanded deny pattern vs compressed live URL
    ("[2001:db8:0:0:0:0:0:1]", "http://[2001:db8::1]/"),
    # compressed deny pattern vs fully-expanded (leading-zero) live URL
    ("[2001:db8::1]", "http://[2001:0db8:0000:0000:0000:0000:0000:1]/"),
    # case-only control: uppercase hextets must still match
    ("[2001:DB8::1]", "http://[2001:db8::1]/"),
]
for pat_in, url in cases:
    norm = _normalize_domain_patterns([pat_in])[0]
    host = _normalize_host(url)
    matched = _matches_domain(host, norm)
    status = "DENY-WORKS" if matched else "FAIL-OPEN"
    print("pattern_in=%-44r normalized=%-28r host=%-20r match=%-5s %s" % (pat_in, norm, host, matched, status))
