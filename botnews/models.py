from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

def now():
    return datetime.now(timezone.utc).isoformat()

def canonical(url):
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_") and k not in {"ref", "fbclid"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(query), ""))

def normalized(text):
    return " ".join(re.findall(r"[\w]+", text.lower()))

@dataclass
class Post:
    source_id: int
    url: str
    title: str
    text: str
    published: str | None = None
    links: list[str] = field(default_factory=list)

    @property
    def fingerprint(self):
        return hashlib.sha256(normalized(self.text).encode()).hexdigest()
