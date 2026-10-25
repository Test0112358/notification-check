"""
Shared test setup: points every data/cache/outbox path at a temporary folder
(before any project module is imported) and provides a fake HTTP session that
serves the HTML fixtures in tests/fixtures/.
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
TMP = tempfile.mkdtemp(prefix="accc-monitor-tests-")

os.environ["ACCC_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["ACCC_CACHE_DIR"] = os.path.join(TMP, ".cache")
os.environ["ACCC_OUTBOX_DIR"] = os.path.join(TMP, "outbox")
os.environ["ACCC_WATCHLIST"] = os.path.join(TMP, "watchlist.json")
os.environ["ACCC_REQUEST_DELAY"] = "0"
os.environ.pop("GITHUB_OUTPUT", None)
os.environ.pop("FULL_REFRESH", None)

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REGISTER = "https://www.accc.gov.au/public-registers/acquisitions-and-mergers-registers/acquisitions-register"
SLUG_ACTIVE = "challenger-channel-capital-and-fidante"
SLUG_COMPLETED = "stripe-openrouter"
SLUG_WAIVER = "one-equity-partners-%E2%80%93-myriad360"


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def reset_dirs():
    for sub in ("data", ".cache", "outbox"):
        path = os.path.join(TMP, sub)
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)
    path = os.environ["ACCC_WATCHLIST"]
    if os.path.exists(path):
        os.remove(path)


class FakeResponse:
    def __init__(self, status_code, body=b"", headers=None):
        self.status_code = status_code
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = self.content.decode("utf-8", errors="replace")
        self.headers = headers or {}


class FakeRequestsSession:
    """Stands in for requests.Session. `pages` maps URL → (status, body)."""

    def __init__(self, pages=None, default_status=404, blocked=False):
        self.headers = {}
        self.pages = pages or {}
        self.default_status = default_status
        self.blocked = blocked
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        if self.blocked:
            return FakeResponse(403, "<H1>Access Denied</H1>")
        status, body = self.pages.get(url, (self.default_status, "Not found"))
        return FakeResponse(status, body)


def site_pages(active_html=None, completed_html=None, waiver_html=None, listing_html=None):
    return {
        f"{REGISTER}?items_per_page=50&page=0": (200, listing_html or fixture("listing_page.html")),
        f"{REGISTER}/{SLUG_ACTIVE}": (200, active_html or fixture("detail_active.html")),
        f"{REGISTER}/{SLUG_COMPLETED}": (200, completed_html or fixture("detail_completed.html")),
        f"{REGISTER}/{SLUG_WAIVER}": (200, waiver_html or fixture("detail_waiver.html")),
        "https://www.accc.gov.au/system/files/public-registers/documents/Questionnaire.docx": (200, b"PK\x03\x04docx"),
        "https://www.accc.gov.au/system/files/public-registers/documents/Challenger%20-%20Remedy%20offer.pdf": (200, b"%PDF-1.7"),
    }
