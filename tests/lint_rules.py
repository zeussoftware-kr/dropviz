"""The no-hard-coding rules as a function, so the test can prove them on must-catch / must-not-catch snippets."""
import re

# protocol or unit constants that are allowed outside defaults.json / the catalogues
ALLOW = (
    r"^\s*DAY = 24 \* 3600$", r"^\s*WEEK = 7 \* DAY$", r"jsonrpc", r"2\.0", r"protocolVersion", r"Content-(Type|Length)", r"Cache-Control",
    r"application/json", r"text/html", r"X-Dropviz-Local", r"Accept-Language", r"utf-8", r"refs/(heads|remotes|notes)", r"dropviz-(verify|tokens)",
)
RULES = (
    ("user-facing print literal", r"print\(\s*f?[\"'][A-Za-z]"),
    ("argparse help literal", r"(help|description)=f?[\"'][A-Za-z]"),
    ("hex colour", r"[\"']#[0-9a-fA-F]{6}[\"']"),
    ("time threshold written as a number", r"\b\d+\s*\*\s*(?:86400|3600)\b|\b\d+\s*\*\s*DAY\b|\b\d+\s*\*\s*60\s*[,)\]]"),
    ("home path literal", r"[\"']~/|[\"']\.claude|[\"']\.codex|\.local/share"),
    ("port or host literal", r"\b127\.0\.0\.1:\d+|port\s*=\s*\d{4}\b"),
    ("error text literal", r"raise \w+Error\(\s*f?[\"'][A-Za-z]"),
)


def find(text):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if any(re.search(a, line) for a in ALLOW):
            continue
        for label, rx in RULES:
            if re.search(rx, line):
                hits.append((n, label, line.strip()))
    return hits
