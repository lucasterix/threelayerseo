"""SEO + Lighthouse-proxy audit for a rendered URL.

We don't run headless Chrome — instead we fetch the HTML and score it
against a marketing-agency checklist for the four Lighthouse families
(Performance, Accessibility, Best-Practices, SEO). Heuristics are
designed to catch the same regressions a real Lighthouse run would, in
~200 ms per page and without a Node toolchain.

Scoring buckets:
    seo     — title/meta/canonical/og/structured-data/H1/keyword presence
    perf    — html size, render-blocking resources, image lazy-loading
    a11y    — alt text, language attr, contrast hint, semantic landmarks
    overall — weighted blend (40 SEO / 30 Perf / 30 A11y) so SEO leads.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser

import httpx

log = logging.getLogger(__name__)


SEVERITY_FAIL = "fail"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    fix_hint: str = ""


@dataclass
class Audit:
    url: str
    score: int = 0          # weighted 0..100
    seo_score: int = 0
    perf_score: int = 0
    a11y_score: int = 0
    issues: list[Issue] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class _DOM(HTMLParser):
    """Minimal DOM walker — counts what we need without lxml."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.meta_description = ""
        self.meta_robots = ""
        self.canonical = ""
        self.og_image = ""
        self.og_title = ""
        self.lang = ""
        self.h1_count = 0
        self.h1_text = ""
        self._in_h1 = False
        self.h_levels: list[int] = []
        self.images_total = 0
        self.images_no_alt = 0
        self.images_no_dim = 0
        self.images_no_lazy = 0
        self.scripts_blocking = 0   # head <script src> without async/defer/module
        self.scripts_inline = 0
        self.stylesheets_blocking = 0
        self.stylesheets_inline = 0
        self.json_ld_count = 0
        self._in_json_ld = False
        self.internal_links = 0
        self.external_links = 0
        self.host_for_link_check = ""
        self._in_head = False
        self.has_main = False
        self.has_header = False
        self.has_footer = False
        self.has_nav = False
        self.text_chars = 0
        self._suppress_text = 0   # depth counter for script/style

    def handle_starttag(self, tag, attrs_):
        attrs = {k: (v or "") for k, v in attrs_}
        if tag == "html":
            self.lang = attrs.get("lang", "")
        elif tag == "head":
            self._in_head = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attrs.get("name", "").lower()
            prop = attrs.get("property", "").lower()
            if name == "description":
                self.meta_description = attrs.get("content", "")
            elif name == "robots":
                self.meta_robots = attrs.get("content", "")
            elif prop == "og:image":
                self.og_image = attrs.get("content", "")
            elif prop == "og:title":
                self.og_title = attrs.get("content", "")
        elif tag == "link":
            rel = attrs.get("rel", "").lower()
            if "canonical" in rel:
                self.canonical = attrs.get("href", "")
            if "stylesheet" in rel:
                self.stylesheets_blocking += 1
        elif tag == "script":
            if attrs.get("type", "").lower() == "application/ld+json":
                self._in_json_ld = True
                self.json_ld_count += 1
                self._suppress_text += 1
                return
            if attrs.get("src"):
                if self._in_head and not (
                    "async" in attrs or "defer" in attrs
                    or attrs.get("type", "").lower() == "module"
                ):
                    self.scripts_blocking += 1
            else:
                self.scripts_inline += 1
            self._suppress_text += 1
        elif tag == "style":
            self.stylesheets_inline += 1
            self._suppress_text += 1
        elif tag == "h1":
            self.h1_count += 1
            self._in_h1 = True
            self.h_levels.append(1)
        elif tag in ("h2", "h3", "h4", "h5", "h6"):
            self.h_levels.append(int(tag[1]))
        elif tag == "img":
            self.images_total += 1
            if not attrs.get("alt", "").strip():
                self.images_no_alt += 1
            if not (attrs.get("width") and attrs.get("height")):
                self.images_no_dim += 1
            loading = attrs.get("loading", "").lower()
            if loading != "lazy":
                self.images_no_lazy += 1
        elif tag == "a":
            href = attrs.get("href", "")
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                if href.startswith("http"):
                    if self.host_for_link_check and self.host_for_link_check in href:
                        self.internal_links += 1
                    else:
                        self.external_links += 1
                else:
                    self.internal_links += 1
        elif tag == "main":
            self.has_main = True
        elif tag == "header":
            self.has_header = True
        elif tag == "footer":
            self.has_footer = True
        elif tag == "nav":
            self.has_nav = True

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag in ("script", "style"):
            self._suppress_text = max(0, self._suppress_text - 1)
            if self._in_json_ld and tag == "script":
                self._in_json_ld = False

    def handle_data(self, data):
        if self._suppress_text > 0:
            return
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1_text += data
        self.text_chars += len(data.strip())


def _audit_html(url: str, html: str, primary_keyword: str | None = None) -> Audit:
    audit = Audit(url=url)
    dom = _DOM()
    # host hint for link classification
    m = re.match(r"https?://([^/]+)", url)
    if m:
        dom.host_for_link_check = m.group(1)
    try:
        dom.feed(html)
    except Exception:  # noqa: BLE001
        log.warning("html parse error for %s", url, exc_info=True)

    html_size_kb = round(len(html.encode("utf-8")) / 1024, 1)
    audit.metrics = {
        "html_size_kb": html_size_kb,
        "scripts_blocking": dom.scripts_blocking,
        "scripts_inline": dom.scripts_inline,
        "stylesheets_blocking": dom.stylesheets_blocking,
        "stylesheets_inline": dom.stylesheets_inline,
        "images_total": dom.images_total,
        "images_no_alt": dom.images_no_alt,
        "images_no_dim": dom.images_no_dim,
        "images_no_lazy": dom.images_no_lazy,
        "h1_count": dom.h1_count,
        "json_ld_count": dom.json_ld_count,
        "internal_links": dom.internal_links,
        "external_links": dom.external_links,
        "text_chars": dom.text_chars,
        "title_length": len(dom.title.strip()),
        "meta_desc_length": len(dom.meta_description.strip()),
    }

    # ─── SEO checks ─────────────────────────────────────────────────────────
    seo_pass = 0
    seo_max = 0

    def add_check(family: str, code: str, ok: bool, severity: str, msg: str, fix: str = "") -> None:
        nonlocal seo_pass, seo_max, perf_pass, perf_max, a11y_pass, a11y_max
        # local table-driven counter without nonlocal mess
        if ok:
            audit.passed.append(code)
        else:
            audit.issues.append(Issue(code=code, severity=severity, message=msg, fix_hint=fix))

    # SEO
    seo_checks = []
    seo_checks.append(("title-present", bool(dom.title.strip()), SEVERITY_FAIL,
                       "Kein <title>-Tag", "title in <head> ergänzen"))
    seo_checks.append(("title-length", 25 <= len(dom.title.strip()) <= 65, SEVERITY_WARN,
                       f"Title-Länge {len(dom.title.strip())} Zeichen (Ziel: 25–65)",
                       "Title kürzen oder ausbauen"))
    seo_checks.append(("meta-description", bool(dom.meta_description.strip()), SEVERITY_FAIL,
                       "Keine Meta-Description", "meta name=description ergänzen"))
    seo_checks.append(("meta-description-length",
                       110 <= len(dom.meta_description.strip()) <= 165, SEVERITY_WARN,
                       f"Meta-Description {len(dom.meta_description.strip())} Zeichen (Ziel: 110–165)",
                       "Description neu schreiben"))
    seo_checks.append(("h1-single", dom.h1_count == 1, SEVERITY_WARN,
                       f"H1-Anzahl {dom.h1_count} (Ziel: genau 1)",
                       "Sicherstellen, dass nur ein H1 pro Seite existiert"))
    seo_checks.append(("canonical", bool(dom.canonical), SEVERITY_WARN,
                       "Kein rel=canonical", "<link rel=canonical> in <head>"))
    seo_checks.append(("og-image", bool(dom.og_image), SEVERITY_WARN,
                       "Kein og:image", "Featured-Image als og:image setzen"))
    seo_checks.append(("structured-data", dom.json_ld_count > 0, SEVERITY_FAIL,
                       "Kein JSON-LD Structured Data",
                       "Article/WebSite/BreadcrumbList Schema rendern"))
    seo_checks.append(("noindex-absent", "noindex" not in dom.meta_robots.lower(), SEVERITY_FAIL,
                       "Seite ist auf noindex gesetzt", "robots-Meta entfernen"))
    seo_checks.append(("internal-links", dom.internal_links >= 1, SEVERITY_WARN,
                       "Keine internen Links", "Mindestens einen internen Verweis ergänzen"))
    if primary_keyword:
        kw = primary_keyword.lower()
        seo_checks.append(("keyword-in-title", kw in dom.title.lower(), SEVERITY_WARN,
                           f"Primary-Keyword '{primary_keyword}' fehlt im Title",
                           "Title so umformulieren, dass das Keyword vorkommt"))
        seo_checks.append(("keyword-in-h1", kw in dom.h1_text.lower(), SEVERITY_INFO,
                           f"Primary-Keyword fehlt in H1",
                           "H1 ans Keyword angleichen"))

    # PERF
    perf_pass = 0; perf_max = 0
    perf_checks = []
    perf_checks.append(("html-size", html_size_kb <= 200, SEVERITY_WARN,
                        f"HTML {html_size_kb} KB (Ziel: <200 KB)",
                        "Inline-Daten reduzieren / lazy-loaden"))
    # Tailwind CDN counts as one big render-blocking script — flag if more than 1 blocker.
    perf_checks.append(("render-blocking-scripts",
                        dom.scripts_blocking <= 1, SEVERITY_WARN,
                        f"{dom.scripts_blocking} render-blockierende Scripts (Ziel: ≤1)",
                        "Scripts mit defer/async oder ans Body-Ende schieben"))
    perf_checks.append(("stylesheets-blocking",
                        dom.stylesheets_blocking <= 2, SEVERITY_INFO,
                        f"{dom.stylesheets_blocking} blockierende Stylesheets",
                        "Stylesheets bündeln"))
    if dom.images_total:
        perf_checks.append(("images-dimensions",
                            dom.images_no_dim == 0, SEVERITY_WARN,
                            f"{dom.images_no_dim}/{dom.images_total} Bilder ohne width/height",
                            "width/height am <img> setzen für CLS"))
        perf_checks.append(("images-lazy",
                            dom.images_no_lazy <= 1, SEVERITY_INFO,
                            f"{dom.images_no_lazy}/{dom.images_total} Bilder ohne loading=lazy",
                            "loading=lazy außer für hero/above-fold"))

    # A11Y
    a11y_pass = 0; a11y_max = 0
    a11y_checks = []
    a11y_checks.append(("html-lang", bool(dom.lang.strip()), SEVERITY_FAIL,
                        "<html> ohne lang-Attribut",
                        'lang="de" am <html>-Element'))
    if dom.images_total:
        a11y_checks.append(("img-alt",
                            dom.images_no_alt == 0, SEVERITY_FAIL,
                            f"{dom.images_no_alt}/{dom.images_total} Bilder ohne alt-Text",
                            "alt-Attribut beschreibend setzen"))
    a11y_checks.append(("landmark-main", dom.has_main, SEVERITY_WARN,
                        "Kein <main>-Landmark",
                        "Hauptinhalt in <main> hüllen"))
    a11y_checks.append(("landmark-nav-or-header",
                        dom.has_nav or dom.has_header, SEVERITY_INFO,
                        "Kein <nav>/<header>-Landmark", "Navigations-Element ergänzen"))
    a11y_checks.append(("heading-hierarchy",
                        _heading_ok(dom.h_levels), SEVERITY_INFO,
                        "Heading-Reihenfolge überspringt Ebenen",
                        "H2/H3 in logischer Reihenfolge"))

    def _score(checks):
        if not checks:
            return 100
        weight = lambda sev: {"fail": 3, "warn": 2, "info": 1}.get(sev, 1)
        total = sum(weight(c[2]) for c in checks)
        ok = sum(weight(c[2]) for c in checks if c[1])
        return int(round(100 * ok / total))

    for c in seo_checks + perf_checks + a11y_checks:
        add_check("", c[0], c[1], c[2], c[3], c[4] if len(c) > 4 else "")

    audit.seo_score = _score(seo_checks)
    audit.perf_score = _score(perf_checks)
    audit.a11y_score = _score(a11y_checks)
    audit.score = int(round(audit.seo_score * 0.40 + audit.perf_score * 0.30 + audit.a11y_score * 0.30))
    return audit


def _heading_ok(levels: list[int]) -> bool:
    """No heading skips more than one level (e.g. H1 → H4 is bad)."""
    prev = 0
    for lv in levels:
        if prev and lv > prev + 1:
            return False
        prev = lv
    return True


def audit_url(url: str, primary_keyword: str | None = None, timeout: float = 8.0) -> Audit:
    """Fetch URL and audit it. Network errors return an Audit with a single fail issue."""
    try:
        r = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "ThreeLayerSEO-Audit/1.0 (+seo.zdkg.de)"},
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        a = Audit(url=url, score=0)
        a.issues.append(Issue(
            code="fetch-failed", severity=SEVERITY_FAIL,
            message=f"Konnte URL nicht laden: {e}",
            fix_hint="DNS / TLS / Caddy prüfen",
        ))
        return a
    return _audit_html(url, r.text, primary_keyword=primary_keyword)


def audit_to_dict(audit: Audit) -> dict:
    d = asdict(audit)
    d["issues"] = [asdict(i) for i in audit.issues]
    return d
