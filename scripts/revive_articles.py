#!/usr/bin/env python3
"""Revive the articles linked from index.html and host copies on the site.

For every article link on the site (links with class="pub"), the script:
  1. unwraps Wayback Machine links back to the original URL,
  2. finds the most recent good capture in the Wayback Machine,
  3. downloads the raw archived page (falls back to the live URL),
  4. extracts the article body and downloads its images,
  5. writes archive/<slug>/index.html (a page on this site with a note
     explaining where the copy came from) plus archive/index.html,
  6. writes articles.md in the site root with every article in Markdown,
  7. points the article links in index.html at the new local pages.

PDFs and paywalled publisher pages are listed with their links, not copied.

Standard library only. Usage:
    python3 scripts/revive_articles.py
    python3 scripts/revive_articles.py --only cheap-absurdity   # trial run, site untouched
    python3 scripts/revive_articles.py --no-update-index
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "revive-articles/1.0 (personal site archive script)"
WAYBACK_PREFIX = re.compile(r"^https?://web\.archive\.org/web/[^/]+/(.+)$")
SKIP_HOSTS = ("doi.org", "repository.tudelft.nl")  # paywalled / PDF repositories

# Set per article: when the text comes from an archived capture, images are
# loaded from the same capture (the originals are usually gone too).
IMG_PREFIX = ""
MD_IMG_BASE = ""  # path prefix for locally saved images, as seen from articles.md


# --------------------------------------------------------------------------- #
# Tiny DOM built with the standard library parser
# --------------------------------------------------------------------------- #

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    def cls(self):
        return (self.attrs.get("class") or "").split()

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def find_all(self, tag=None, cls=None):
        return [n for n in self.iter()
                if (tag is None or n.tag == tag) and (cls is None or cls in n.cls())]

    def find(self, tag=None, cls=None):
        found = self.find_all(tag, cls)
        return found[0] if found else None

    def text(self):
        out = []
        for c in self.children:
            out.append(c.text() if isinstance(c, Node) else c)
        return "".join(out)


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(markup):
    b = TreeBuilder()
    b.feed(markup)
    b.close()
    return b.root


# --------------------------------------------------------------------------- #
# Reading the site
# --------------------------------------------------------------------------- #

def unwrap(url):
    m = WAYBACK_PREFIX.match(url)
    return m.group(1) if m else url


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


def collect_articles(index_path):
    """Return [{title, url, date, venue}] for every article link in index.html."""
    root = parse(index_path.read_text(encoding="utf-8"))
    seen, items = set(), []
    for row in root.find_all("div", "row"):
        when = row.find("p", "when")
        for a in row.find_all("a", "pub"):
            url = unwrap(a.attrs.get("href", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            venue = row.find("em", "where")
            items.append({
                "title": squash(a.text()).strip("“”\""),
                "url": url,
                "date": squash(when.text()) if when else "",
                "venue": squash(venue.text()) if venue else "",
            })
    return items


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

def fetch(url, retries=4, delay=2.0):
    """GET url and return (bytes, content-type). Retries on 429/5xx/network errors."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(delay * (2 ** attempt))
    raise last


def latest_capture(url, wayback):
    """Return (timestamp, original) of the newest 200 capture, or None."""
    q = urllib.parse.urlencode({
        "url": url, "output": "json", "filter": "statuscode:200",
        "fl": "timestamp,original", "limit": "-1",
    })
    try:
        body, _ = fetch(f"{wayback}/cdx/search/cdx?{q}")
    except Exception as e:  # noqa: BLE001
        print(f"    CDX lookup failed: {e}", file=sys.stderr)
        return None
    try:
        rows = json.loads(body or b"[]")
    except json.JSONDecodeError:
        return None
    rows = [r for r in rows if r and r[0] != "timestamp"]  # drop header row
    return (rows[-1][0], rows[-1][1]) if rows else None


def available_capture(url, api="https://archive.org/wayback/available"):
    """Fallback lookup through archive.org's availability API."""
    try:
        body, _ = fetch(f"{api}?" + urllib.parse.urlencode({"url": url}), retries=2)
        snap = json.loads(body).get("archived_snapshots", {}).get("closest") or {}
    except Exception as e:  # noqa: BLE001
        print(f"    availability lookup failed: {e}", file=sys.stderr)
        return None
    if snap.get("available") and str(snap.get("status")) == "200":
        return snap["timestamp"], url
    return None


def decode(body, ctype):
    m = re.search(r"charset=([\w-]+)", ctype or "")
    for enc in ([m.group(1)] if m else []) + ["utf-8", "latin-1"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Article extraction and Markdown conversion
# --------------------------------------------------------------------------- #

DROP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form",
        "button", "svg", "iframe", "template", "select", "input"}
# matched against whole words inside class names / ids ("sidebar-right", "post_meta")
BOILERPLATE = re.compile(r"(?:^|[-_])(nav|navbar|menu|footer|header|share|sharing|social|"
                         r"cookie|cookies|related|comment|comments|sidebar|newsletter|"
                         r"subscribe|breadcrumb|breadcrumbs|byline|avatar|author|meta|"
                         r"hero|tags|categories)(?:[-_]|$)", re.I)


def is_noise(n):
    if n.tag in DROP:
        return True
    if n.tag in ("body", "main", "article"):
        return False
    tokens = n.cls() + ([n.attrs["id"]] if n.attrs.get("id") else [])
    return any(BOILERPLATE.search(t) for t in tokens)


def page_title(root):
    for n in root.find_all("meta"):
        if n.attrs.get("property") == "og:title" and n.attrs.get("content"):
            return squash(n.attrs["content"])
    h1 = root.find("h1")
    if h1 and squash(h1.text()):
        return squash(h1.text())
    t = root.find("title")
    return squash(t.text()) if t else ""


def paragraph_score(n):
    return sum(len(squash(p.text())) for p in n.find_all("p") if not any(
        is_noise(a) for a in ancestors(p, stop=n)))


def ancestors(n, stop=None):
    p = n.parent
    while p is not None and p is not stop:
        yield p
        p = p.parent


def main_content(root):
    """Pick <article>, else <main>, else the element holding the most paragraph text."""
    for tag in ("article", "main"):
        cands = [n for n in root.find_all(tag) if paragraph_score(n) > 200]
        if cands:
            return max(cands, key=paragraph_score)
    best, best_score = root, 0
    for n in root.iter():
        if n.tag in ("div", "section", "td") and not is_noise(n):
            # score by direct-ish paragraphs so <body> doesn't always win
            s = sum(len(squash(p.text())) for p in n.children
                    if isinstance(p, Node) and p.tag in ("p", "blockquote", "h2", "h3", "ul", "ol"))
            if s > best_score:
                best, best_score = n, s
    if best is root or best_score < 200:
        body = root.find("body")
        return body or root
    # climb while the parent adds more paragraph text (article split into blocks)
    while best.parent is not None and best.parent.tag in ("div", "section") \
            and paragraph_score(best.parent) > paragraph_score(best) * 1.3:
        best = best.parent
    return best


def absolute(url, base):
    if not url:
        return url
    url = urllib.parse.urljoin(base, url)
    return unwrap(url)


def image_url(img, base):
    """Where an <img> points in Markdown: the saved local copy if there is one."""
    if img.attrs.get("data-local"):
        return MD_IMG_BASE + img.attrs["src"]
    src = absolute(img.attrs.get("src"), base)
    return f"{IMG_PREFIX}{src}" if src and IMG_PREFIX and not src.startswith("data:") else src


def inline_md(n, base):
    out = []
    for c in n.children:
        if not isinstance(c, Node):
            out.append(re.sub(r"\s+", " ", c))
            continue
        if is_noise(c):
            continue
        t = c.tag
        inner = inline_md(c, base)
        if t in ("em", "i"):
            out.append(f"*{inner.strip()}*" if inner.strip() else "")
        elif t in ("strong", "b"):
            out.append(f"**{inner.strip()}**" if inner.strip() else "")
        elif t == "a":
            href = absolute(c.attrs.get("href"), base)
            out.append(f"[{inner.strip()}]({href})" if href and inner.strip() else inner)
        elif t == "br":
            out.append("  \n")
        elif t == "img":
            src = image_url(c, base)
            if src:
                out.append(f"![{c.attrs.get('alt', '')}]({src})")
        elif t == "code":
            out.append(f"`{inner}`")
        else:
            out.append(inner)
    return "".join(out)


def block_md(n, base, depth=0):
    blocks = []
    inline_buf = []

    def flush():
        s = squash("".join(inline_buf)) if inline_buf else ""
        if s:
            blocks.append(s)
        inline_buf.clear()

    for c in n.children:
        if not isinstance(c, Node):
            inline_buf.append(re.sub(r"\s+", " ", c))
            continue
        if is_noise(c):
            continue
        t = c.tag
        if t == "h1":
            flush()
            continue  # the article title is already printed above the text
        if t in ("h2", "h3", "h4", "h5", "h6"):
            flush()
            level = min(int(t[1]) + 2, 6)  # article title is ###, so headings nest below it
            txt = squash(inline_md(c, base))
            if txt:
                blocks.append("#" * level + " " + txt)
        elif t == "p":
            flush()
            txt = inline_md(c, base).strip()
            txt = re.sub(r"[ \t]+(?=[^ \t\n])", " ", txt)   # keep "  \n" hard breaks
            txt = re.sub(r"\n[ \t]+", "\n", txt)
            if txt:
                blocks.append(txt)
        elif t in ("ul", "ol"):
            flush()
            items = []
            for i, li in enumerate([x for x in c.children if isinstance(x, Node) and x.tag == "li"], 1):
                bullet = f"{i}." if t == "ol" else "-"
                items.append("  " * depth + f"{bullet} {squash(inline_md(li, base))}")
            if items:
                blocks.append("\n".join(items))
        elif t == "blockquote":
            flush()
            inner = block_md(c, base, depth)
            if inner:
                blocks.append("\n".join("> " + ln if ln else ">" for ln in inner.splitlines()))
        elif t == "figure":
            flush()
            img = c.find("img")
            cap = c.find("figcaption")
            if img and (img.attrs.get("src") or img.attrs.get("data-local")):
                alt = squash(cap.text()) if cap else img.attrs.get("alt", "")
                blocks.append(f"![{alt}]({image_url(img, base)})")
                if cap and squash(cap.text()):
                    blocks.append(f"*{squash(cap.text())}*")
        elif t == "img":
            flush()
            if c.attrs.get("src"):
                blocks.append(f"![{c.attrs.get('alt', '')}]({image_url(c, base)})")
        elif t == "hr":
            flush()
            blocks.append("---")
        elif t in ("pre",):
            flush()
            blocks.append("```\n" + c.text().strip("\n") + "\n```")
        elif t in ("div", "section", "article", "main", "body", "span", "td", "tr",
                   "table", "tbody", "center", "font"):
            # span/font may be inline: only treat as block if it contains blocks
            if t in ("span", "font") and not any(
                    isinstance(x, Node) and x.tag in ("p", "div", "h1", "h2", "h3", "ul", "ol")
                    for x in c.iter()):
                inline_buf.append(inline_md(c, base))
                continue
            flush()
            inner = block_md(c, base, depth)
            if inner:
                blocks.append(inner)
        else:
            wrapper = Node("#wrap")
            wrapper.children = [c]
            inline_buf.append(inline_md(wrapper, base))
    flush()
    return "\n\n".join(b for b in blocks if b.strip())


def to_markdown_node(body, base):
    md = block_md(body, base)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def to_markdown(markup, base):
    root = parse(markup)
    return page_title(root), to_markdown_node(main_content(root), base)


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #

IMG_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif"}


def best_src(img):
    """Real image URL, looking past lazy-loading placeholders and srcset."""
    for key in ("data-src", "data-lazy-src", "data-original"):
        if img.attrs.get(key):
            return img.attrs[key]
    srcset = img.attrs.get("srcset") or img.attrs.get("data-srcset")
    if srcset:
        cands = []
        for part in srcset.split(","):
            bits = part.strip().split()
            if bits:
                w = re.match(r"(\d+)", bits[1]) if len(bits) > 1 else None
                cands.append((int(w.group(1)) if w else 0, bits[0]))
        if cands:
            return max(cands)[1]
    src = img.attrs.get("src", "")
    return "" if src.startswith("data:") else src


def save_images(body, base, folder, wayback, ts):
    """Download every image in the article into folder/images and point the
    <img> nodes at the local files. Images that fail to download are removed."""
    count, saved = 0, {}
    for img in [n for n in body.iter() if n.tag in ("img", "source")]:
        if img.tag == "source":  # <picture><source> alternatives: keep only the <img>
            img.tag = "#drop"
            continue
        if any(is_noise(a) for a in ancestors(img, stop=body)):
            img.tag = "#drop"  # avatars, share icons, headers: not part of the article
            continue
        src = absolute(best_src(img), base)
        if not src:
            img.tag = "#drop"
            continue
        if src in saved:  # same picture used twice: reuse the saved file
            img.attrs = {"src": saved[src], "alt": img.attrs.get("alt", ""), "data-local": "1"}
            continue
        url = f"{wayback}/web/{ts}im_/{src}" if ts else src
        try:
            data, ctype = fetch(url, retries=3)
        except Exception as e:  # noqa: BLE001
            print(f"    image failed ({src}): {e}", file=sys.stderr)
            img.tag = "#drop"
            continue
        ctype = (ctype or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            img.tag = "#drop"
            continue
        count += 1
        stem = re.sub(r"[^a-z0-9]+", "-",
                      Path(urllib.parse.urlparse(src).path).stem.lower()).strip("-")[:50]
        name = f"{count:02d}-{stem or 'image'}{IMG_TYPES.get(ctype, Path(src).suffix or '.img')}"
        (folder / "images").mkdir(parents=True, exist_ok=True)
        (folder / "images" / name).write_bytes(data)
        saved[src] = f"images/{name}"
        img.attrs = {"src": saved[src], "alt": img.attrs.get("alt", ""), "data-local": "1"}
    return count


# --------------------------------------------------------------------------- #
# HTML output (sanitised: only plain content tags, no scripts or styling)
# --------------------------------------------------------------------------- #

KEEP = {"p", "h2", "h3", "h4", "h5", "h6", "a", "em", "i", "strong", "b", "ul", "ol",
        "li", "blockquote", "figure", "figcaption", "img", "br", "hr", "pre", "code",
        "sup", "sub", "cite", "q", "table", "thead", "tbody", "tr", "th", "td"}
VOID_OUT = {"img", "br", "hr"}


def to_html(n, base):
    out = []
    for c in n.children:
        if not isinstance(c, Node):
            out.append(html.escape(c, quote=False))
            continue
        if c.tag in ("#drop", "h1") or is_noise(c):
            continue
        if c.tag not in KEEP:  # unwrap div/span/section/etc.
            out.append(to_html(c, base))
            continue
        attrs = ""
        if c.tag == "a":
            href = absolute(c.attrs.get("href"), base)
            if href and not href.startswith("javascript:"):
                attrs = f' href="{html.escape(href)}"'
        elif c.tag == "img":
            if not c.attrs.get("data-local"):
                continue  # never hotlink: only locally saved images are shown
            attrs = (f' src="{html.escape(c.attrs["src"])}"'
                     f' alt="{html.escape(c.attrs.get("alt", ""))}" loading="lazy"')
        if c.tag in VOID_OUT:
            out.append(f"<{c.tag}{attrs}>")
        else:
            inner = to_html(c, base)
            if c.tag in ("p", "li", "h2", "h3", "h4", "h5", "h6", "figcaption") and not inner.strip():
                continue
            out.append(f"<{c.tag}{attrs}>{inner}</{c.tag}>")
    return "".join(out)


def pretty(markup):
    """Put block elements on their own lines so the saved pages are readable."""
    markup = re.sub(r"\s*(</?(?:p|h[2-6]|ul|ol|li|blockquote|figure|figcaption|hr|table|tr|pre)[^>]*>)\s*",
                    r"\n\1", markup)
    markup = re.sub(r"\n(</(?:p|h[2-6]|li|figcaption|pre)>)", r"\1", markup)
    markup = re.sub(r"[ \t]+", " ", markup)
    return "\n".join(ln.strip() for ln in markup.strip().splitlines() if ln.strip())


# --------------------------------------------------------------------------- #
# Fetching one article
# --------------------------------------------------------------------------- #

def slug_for(url):
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return re.sub(r"[^a-z0-9]+", "-", path.rsplit("/", 1)[-1].lower()).strip("-") or "article"


def revive(item, wayback):
    url = item["url"]
    host = urllib.parse.urlparse(url).netloc
    if url.lower().endswith(".pdf"):
        return {"status": "pdf", "note": f"PDF, not copied: <{url}>"}
    if any(h in host for h in SKIP_HOSTS):
        return {"status": "skipped", "note": f"Publisher/repository page, not copied: <{url}>"}

    cap = latest_capture(url, wayback)
    if not cap and wayback == "https://web.archive.org":
        cap = available_capture(url)
    sources = []
    if cap:
        ts, original = cap
        sources.append((f"{wayback}/web/{ts}id_/{original}",
                        f"https://web.archive.org/web/{ts}/{original}", ts))
    sources.append((url, url, None))  # live page as fallback

    global IMG_PREFIX
    for raw_url, cite_url, ts in sources:
        IMG_PREFIX = f"{wayback}/web/{ts}im_/" if ts else ""
        try:
            # archived copies get full retries; the live fallback is tried once
            body, ctype = fetch(raw_url, retries=4 if ts else 1)
        except Exception as e:  # noqa: BLE001
            print(f"    fetch failed ({raw_url}): {e}", file=sys.stderr)
            continue
        if "html" not in (ctype or "html"):
            continue
        root = parse(decode(body, ctype))
        node = main_content(root)
        if len(to_markdown_node(node, url)) < 200:
            print(f"    too little text extracted from {raw_url}", file=sys.stderr)
            continue
        return {"status": "ok", "page_title": page_title(root), "node": node, "ts": ts,
                "source": cite_url,
                "captured": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if ts else "live page"}
    return {"status": "missing", "note": f"No archived copy found for <{url}>"}


# --------------------------------------------------------------------------- #
# Writing the site pages
# --------------------------------------------------------------------------- #

def nice_date(iso):
    try:
        y, m, d = iso.split("-")
        months = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"]
        return f"{int(d)} {months[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return iso


def note_for(item, res):
    host = urllib.parse.urlparse(item["url"]).netloc
    when = nice_date(res["captured"])
    if "bnieuws" in host:
        lead = ("<strong>Bnieuws</strong>, the magazine of TU Delft's Faculty of Architecture, "
                "no longer has a working website: bnieuws.nl now redirects to an unrelated site, "
                "so its archive of articles is effectively gone.")
    else:
        lead = "The original page is no longer reliably available."
    if res["ts"]:
        how = (f"This copy was recovered from the Internet Archive's "
               f'<a href="{html.escape(res["source"])}">Wayback Machine</a> '
               f"(captured {when}) so the article stays readable.")
    else:
        lead = ("This article first appeared in <strong>Bnieuws</strong>, the magazine of TU Delft's "
                "Faculty of Architecture, whose website is no longer working.")
        how = (f'This copy was saved from the version republished on '
               f'<a href="{html.escape(item["url"])}">{html.escape(host)}</a>, which is still online.')
    return f"{lead} {how}"


PAGE = """<!DOCTYPE html>
<html lang="en">

<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <title>{title} — Oliwia Jackowska</title>
  <meta name="description" content="{description}">
  <link rel="stylesheet" href="{root}main.css">
  <link rel="icon" type="image/svg+xml" href="/assets/icons/favicon.svg">
</head>

<body class="archive">
  <main class="archive-main">
    <p class="archive-back"><a href="{back_href}">{back_text}</a></p>
{body}
  </main>
</body>

</html>
"""


def write_article_page(item, res, folder, content_html):
    meta = " · ".join(x for x in (item["venue"] and f"<em>{html.escape(item['venue'])}</em>",
                                  html.escape(item["date"])) if x)
    publication = "Bnieuws"  # every recovered article first ran in Bnieuws
    body = f"""    <aside class="archive-note">
      <p>{note_for(item, res)}</p>
    </aside>
    <article class="archive-article">
      <header>
        <p class="archive-meta">{html.escape(publication)} · {meta}</p>
        <h1>{html.escape(item["title"])}</h1>
        <p class="archive-byline">by Oliwia Jackowska</p>
      </header>
{content_html}
    </article>
    <p class="archive-back"><a href="../">More recovered articles</a> · <a href="../../">Oliwia Jackowska</a></p>"""
    page = PAGE.format(title=html.escape(item["title"]),
                       description=html.escape(f"{item['title']} — article by Oliwia Jackowska, "
                                               f"recovered from the Wayback Machine."),
                       root="../../", back_href="../../", back_text="← Oliwia Jackowska", body=body)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(page, encoding="utf-8")


def write_archive_index(results, outdir):
    rows = []
    for item, res in results:
        if res["status"] != "ok":
            continue
        venue = f" in <em>{html.escape(item['venue'])}</em>" if item["venue"] else ""
        rows.append(f'      <li><a href="{res["slug"]}/">{html.escape(item["title"])}</a>'
                    f'<span class="archive-meta"> {html.escape(item["date"])}{venue}</span></li>')
    body = f"""    <h1>Recovered articles</h1>
    <aside class="archive-note">
      <p><strong>Bnieuws</strong>, the magazine of TU Delft's Faculty of Architecture, no longer
        has a working website: bnieuws.nl now redirects to an unrelated site, so its archive of
        articles is effectively gone. These are copies of my own articles, recovered from the
        Internet Archive's Wayback Machine so they stay readable.</p>
    </aside>
    <ul class="archive-list">
{chr(10).join(rows)}
    </ul>"""
    page = PAGE.format(title="Recovered articles",
                       description="Articles by Oliwia Jackowska recovered from the Wayback Machine.",
                       root="../", back_href="../", back_text="← Oliwia Jackowska", body=body)
    (outdir / "index.html").write_text(page, encoding="utf-8")


def render_markdown(results):
    out = ["# Articles", "",
           "Copies of my articles whose original pages are offline. Bnieuws "
           "(the TU Delft architecture faculty magazine) no longer has a working website, "
           "so these were recovered from the [Wayback Machine](https://web.archive.org/). "
           "Each one is also on the site under [archive/](archive/).",
           "", "## Contents", ""]
    for item, _ in results:
        out.append(f"- [{item['title']}](#{slug_for(item['url'])})")
    out.append("")
    for item, res in results:
        out += ["---", "", f'<a id="{slug_for(item["url"])}"></a>', "", f"### {item['title']}", ""]
        meta = [x for x in (item["date"], f"in *{item['venue']}*" if item["venue"] else "") if x]
        if meta:
            out += [" · ".join(meta), ""]
        if res["status"] == "ok":
            out += [f"Original: <{item['url']}>  ",
                    f"Archived copy: <{res['source']}> ({res['captured']})  ",
                    f"On this site: [archive/{res['slug']}/](archive/{res['slug']}/)", "",
                    res["markdown"], ""]
        else:
            out += [f"_{res['note']}_", ""]
    return "\n".join(out).rstrip() + "\n"


def update_index(index_path, results):
    """Point article links in index.html at the local copies."""
    text = index_path.read_text(encoding="utf-8")
    changed = 0
    for item, res in results:
        if res["status"] != "ok" or not res["ts"]:
            continue  # live originals keep their own links
        url = re.escape(item["url"])
        pattern = re.compile(r'(<a class="pub" href=")(?:https?://web\.archive\.org/web/[^/"]+/)?' + url + '"')
        text, n = pattern.subn(rf'\g<1>archive/{res["slug"]}/"', text)
        changed += n
    index_path.write_text(text, encoding="utf-8")
    return changed


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    global MD_IMG_BASE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--index", default=str(ROOT / "index.html"))
    ap.add_argument("-o", "--output", default=str(ROOT / "articles.md"))
    ap.add_argument("--archive-dir", default=str(ROOT / "archive"))
    ap.add_argument("--only", help="only process URLs containing this text")
    ap.add_argument("--no-update-index", action="store_true",
                    help="don't point the links in index.html at the local copies")
    ap.add_argument("--wayback", default="https://web.archive.org",
                    help="Wayback Machine base URL (for testing)")
    ap.add_argument("--pause", type=float, default=1.5,
                    help="seconds to wait between articles (be polite to archive.org)")
    args = ap.parse_args()

    index_path = Path(args.index)
    outdir = Path(args.archive_dir)
    wayback = args.wayback.rstrip("/")
    items = collect_articles(index_path)
    if args.only:
        items = [i for i in items if args.only in i["url"]]
    if not items:
        sys.exit("No article links found.")

    results = []
    for n, item in enumerate(items, 1):
        print(f"[{n}/{len(items)}] {item['title']}\n    {item['url']}")
        res = revive(item, wayback)
        if res["status"] == "ok":
            res["slug"] = slug_for(item["url"])
            folder = outdir / res["slug"]
            if (folder / "images").exists():
                for old in (folder / "images").iterdir():
                    old.unlink()
            imgs = save_images(res["node"], item["url"], folder, wayback, res["ts"])
            content = pretty(to_html(res["node"], item["url"]))
            content = "\n".join("      " + ln for ln in content.splitlines())
            write_article_page(item, res, folder, content)
            MD_IMG_BASE = f"{outdir.name}/{res['slug']}/"
            res["markdown"] = to_markdown_node(res["node"], item["url"])
            print(f"    -> ok ({res['captured']}, {imgs} images) -> {folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder}/")
        else:
            print(f"    -> {res['status']}")
        results.append((item, res))
        if n < len(items):
            time.sleep(args.pause)

    ok = [r for _, r in results if r["status"] == "ok"]
    if ok and not args.only:
        write_archive_index(results, outdir)
    Path(args.output).write_text(render_markdown(results), encoding="utf-8")
    print(f"\nWrote {args.output}: {len(ok)}/{len(results)} articles recovered.")
    if ok and not args.no_update_index and not args.only:
        print(f"Updated {update_index(index_path, results)} links in {index_path.name}.")


if __name__ == "__main__":
    main()
