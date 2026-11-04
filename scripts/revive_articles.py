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
import shutil
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
            # data-original remembers the source once a link points at a local copy
            url = a.attrs.get("data-original") or unwrap(a.attrs.get("href", ""))
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


def captures(url, wayback, limit=12):
    """Distinct 200/HTML captures of url in the Wayback Machine, newest first."""
    q = urllib.parse.urlencode({
        "url": url, "output": "json", "fl": "timestamp,original",
        "filter": ["statuscode:200", "mimetype:text/html"], "collapse": "digest",
    }, doseq=True)
    try:
        body, _ = fetch(f"{wayback}/cdx/search/cdx?{q}")
        rows = json.loads(body or b"[]")
    except Exception as e:  # noqa: BLE001
        print(f"    CDX lookup failed: {e}", file=sys.stderr)
        return []
    rows = [(r[0], r[1]) for r in rows if r and r[0] != "timestamp"]
    return rows[::-1][:limit]


def available_captures(url, api="https://archive.org/wayback/available"):
    """Fallback lookup through archive.org's availability API: the capture
    closest to now and to a few earlier years (before the domain died)."""
    found = []
    for year in (None, "2024", "2023", "2022", "2021"):
        params = {"url": url}
        if year:
            params["timestamp"] = year
        try:
            body, _ = fetch(f"{api}?" + urllib.parse.urlencode(params), retries=2)
            snap = json.loads(body).get("archived_snapshots", {}).get("closest") or {}
        except Exception as e:  # noqa: BLE001
            print(f"    availability lookup failed: {e}", file=sys.stderr)
            continue
        if snap.get("available") and str(snap.get("status")) == "200":
            if snap["timestamp"] not in [t for t, _ in found]:
                found.append((snap["timestamp"], url))
        time.sleep(1)
    return sorted(found, reverse=True)


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

DROP = {"script", "style", "nav", "header", "footer", "aside", "form",
        "button", "svg", "iframe", "template", "select", "input"}
# matched against whole words inside class names / ids ("sidebar-right", "post_meta")
BOILERPLATE = re.compile(r"(?:^|[-_])(nav|navbar|menu|footer|header|share|sharing|social|"
                         r"cookie|cookies|related|comment|comments|sidebar|newsletter|"
                         r"subscribe|breadcrumb|breadcrumbs|byline|avatar|author|meta|"
                         r"hero|tags|categories|teaser|teasers|recommended|read-more|"
                         r"more-articles|pagination|newsletter-signup)(?:[-_]|$)", re.I)

# Pages a dead domain serves instead of the article (registrar blocks, parking, spam)
NOT_AN_ARTICLE = re.compile(r"toegang geweigerd|domain has been blocked|domeinnaam .{0,40}geblokkeerd|"
                            r"this domain (?:is|may be) for sale|buy this domain|domain parking|"
                            r"parked (?:free|domain)|online casino", re.I)


# Blocks whose class looks like clutter ("post-wrap sidebar-on") but that hold
# most of the page's text: layout wrappers, not sidebars. Filled per page.
LAYOUT_WRAPPERS = set()


def is_noise(n):
    if n.tag in DROP:
        return True
    if id(n) in LAYOUT_WRAPPERS:
        return False
    if n.tag == "noscript":
        return n.find("img") is None
    if n.tag in ("body", "main", "article"):
        return False
    tokens = n.cls() + ([n.attrs["id"]] if n.attrs.get("id") else [])
    # "RelatedArticles" / "Article_related__x9" -> "related-articles" / "article-related-x9"
    tokens = [re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", t).lower() for t in tokens]
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
    """The smallest block that holds most of the page's paragraph text.

    Paragraph text inside navigation, footers, sidebars etc. doesn't count.
    This copes with sites that put only the intro inside <article> and the
    body text in a sibling block."""
    body = root.find("body") or root
    LAYOUT_WRAPPERS.clear()
    raw = {}
    for p in body.find_all("p"):
        length = len(squash(p.text()))
        for a in ancestors(p, stop=body.parent):
            raw[id(a)] = raw.get(id(a), 0) + length
    for n in body.iter():
        if n.tag not in DROP and raw.get(id(n), 0) >= 0.5 * raw.get(id(body), 1) and is_noise(n):
            LAYOUT_WRAPPERS.add(id(n))
    score = {}
    for p in body.find_all("p"):
        length = len(squash(p.text()))
        if not length:
            continue
        chain = [p] + list(ancestors(p, stop=body.parent))
        if any(is_noise(a) for a in chain):
            continue
        for a in chain[1:]:
            score[id(a)] = score.get(id(a), 0) + length
    total = score.get(id(body), 0)
    if total < 200:
        return body
    size = {}
    for n in body.iter():
        for a in [n] + list(ancestors(n, stop=body.parent)):
            size[id(a)] = size.get(id(a), 0) + 1
    good = [n for n in body.iter()
            if n.tag in ("div", "section", "article", "main", "td", "body")
            and score.get(id(n), 0) >= 0.8 * total]
    if not good:
        return body
    best = min(good, key=lambda n: size[id(n)])
    # take in the surrounding block when it only adds a little text (usually the
    # intro/standfirst kept beside the body), never a whole extra section
    while best is not body and best.parent is not None:
        mine, theirs = score.get(id(best), 0), score.get(id(best.parent), 0)
        if theirs == mine or theirs - mine > 0.25 * mine:
            break
        best = best.parent
    return best


# Domains that have been taken over since the articles were written: links to
# them are sent to the Wayback Machine instead (bnieuws.nl now redirects to spam).
DEAD_DOMAINS = ("bnieuws.nl",)


def absolute(url, base):
    if not url:
        return url
    url = urllib.parse.urljoin(base, url)
    return unwrap(url)


def safe_link(url, base):
    """Absolute link for the recovered text, rerouting dead domains to the archive."""
    url = absolute(url, base)
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if any(host == d or host.endswith("." + d) for d in DEAD_DOMAINS):
        return f"https://web.archive.org/web/2023/{url}"
    return url


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
            out.append(re.sub(r"\s+", " ", c.translate(INVISIBLE)))
            continue
        if is_noise(c) or c.tag == "#drop":
            continue
        t = c.tag
        inner = inline_md(c, base)
        if t in ("em", "i"):
            out.append(f"*{inner.strip()}*" if inner.strip() else "")
        elif t in ("strong", "b"):
            out.append(f"**{inner.strip()}**" if inner.strip() else "")
        elif t == "a":
            href = safe_link(c.attrs.get("href"), base)
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
            inline_buf.append(re.sub(r"\s+", " ", c.translate(INVISIBLE)))
            continue
        if is_noise(c) or c.tag == "#drop":
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
            if img and img.attrs.get("data-local"):
                alt = squash(cap.text()) if cap else img.attrs.get("alt", "")
                blocks.append(f"![{alt}]({image_url(img, base)})")
            if cap and squash(cap.text()):
                blocks.append(f"*{squash(cap.text())}*")
        elif t == "img":
            flush()
            if c.attrs.get("data-local"):
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
# Cleaning site chrome out of the article
# --------------------------------------------------------------------------- #

INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def same_page(href, base):
    """True for links that only jump within the page (#, #top, page#...)."""
    if not href:
        return False
    full = unwrap(urllib.parse.urljoin(base, href))
    norm = lambda u: re.sub(r"^https?://(www\.)?", "", urllib.parse.urldefrag(u)[0]).rstrip("/")
    return href.startswith("#") or norm(full) == norm(base)


def text_of(n):
    return squash((n.text() if isinstance(n, Node) else n).translate(INVISIBLE))


def prune_outside(node, start=None, end=None):
    """Drop everything before `start` (and start itself) and from `end` on,
    in document order, at every depth."""
    state = {"started": start is None, "ended": False}

    def walk(n):
        kept = []
        for c in n.children:
            if state["ended"]:
                break
            if c is end:
                state["ended"] = True
                break
            if not isinstance(c, Node):
                if state["started"]:
                    kept.append(c)
                continue
            if c is start:
                state["started"] = True
                continue
            if state["started"] or (start is not None and any(x is start for x in c.iter())):
                walk(c)
                kept.append(c)
        n.children = kept

    walk(node)


def clean_article(node, base):
    """Remove the bits of the website that came along with the article: the
    'From the editors / name' header, banners above it, 'Return to top' and
    the teaser cards after it, and links that only jump within the page."""
    start = end = None
    for n in node.iter():
        if n.tag in ("h3", "h4", "h5", "h6") and text_of(n).lower() == "from the editors":
            sib = n.parent.children[n.parent.children.index(n) + 1:]
            nxt = next((x for x in sib if isinstance(x, Node)), None)
            start = nxt if nxt is not None and nxt.tag == n.tag and len(text_of(nxt)) < 60 else n
            break
    for n in node.iter():
        if n.tag == "a" and (text_of(n).lower().startswith("return to top")
                             or (n.attrs.get("href", "").endswith("#top") and not text_of(n))):
            end = n
            break
    if start is not None or end is not None:
        prune_outside(node, start, end)
    # unwrap same-page links (keep what's inside them, e.g. lightbox images)
    for n in list(node.iter()):
        if n.tag == "a" and same_page(n.attrs.get("href"), base):
            n.tag, n.attrs = "span", {}
    attach_captions(node)
    return node


def attach_captions(node):
    """A standalone image followed by a short paragraph (lightbox images put
    their caption there) becomes a <figure> with that paragraph as caption."""
    for img in [n for n in node.iter() if n.tag == "img"]:
        if any(a.tag in ("figure", "a") for a in ancestors(img, stop=node)):
            continue
        box = img
        while box.parent is not node and box.parent.tag == "span" and \
                len([c for c in box.parent.children if isinstance(c, Node) or c.strip()]) == 1:
            box = box.parent
        siblings = box.parent.children
        after = siblings[siblings.index(box) + 1:]
        nxt = next((c for c in after if isinstance(c, Node) or text_of(c)), None)
        if not (isinstance(nxt, Node) and nxt.tag == "p" and 0 < len(text_of(nxt)) <= 140
                and nxt.find("img") is None and nxt.find("a") is None):
            continue
        fig = Node("figure", parent=box.parent)
        cap = Node("figcaption", parent=fig)
        cap.children = nxt.children
        box.parent = fig
        fig.children = [box, cap]
        i = siblings.index(box)
        siblings[i] = fig
        siblings.remove(nxt)


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #

IMG_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif"}


def is_dead(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return any(host == d or host.endswith("." + d) for d in DEAD_DOMAINS)


def unproxy(src):
    """/_next/image?url=<original>&w=... -> <original> (Next.js image optimiser)."""
    parts = urllib.parse.urlparse(src)
    if parts.path.endswith("/_next/image"):
        inner = urllib.parse.parse_qs(parts.query).get("url")
        if inner:
            return inner[0]
    return src


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


def image_sources(src, wayback, ts):
    """Where to try downloading an image from, in order."""
    archived = [f"{wayback}/web/{ts}im_/{src}"] if ts else []
    return archived if is_dead(src) else [src] + archived


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
        src = absolute(unproxy(absolute(best_src(img), base)), base)
        if not src:
            img.tag = "#drop"
            continue
        if src in saved:  # same picture used twice: reuse the saved file
            img.attrs = {"src": saved[src], "alt": img.attrs.get("alt", ""), "data-local": "1"}
            continue
        data = ctype = None
        for url in image_sources(src, wayback, ts):
            try:
                data, ctype = fetch(url, retries=3)
            except Exception as e:  # noqa: BLE001
                print(f"    image failed ({url}): {e}", file=sys.stderr)
                continue
            if (ctype or "").lower().startswith("image/"):
                break
            data = None
        if data is None:
            img.tag = "#drop"
            continue
        ctype = (ctype or "").split(";")[0].strip().lower()
        count += 1
        stem = re.sub(r"[^a-z0-9]+", "-",
                      Path(urllib.parse.urlparse(src).path).stem.lower()).strip("-")[:50]
        name = f"{count:02d}-{stem or 'image'}{IMG_TYPES.get(ctype, Path(src).suffix or '.img')}"
        (folder / "images").mkdir(parents=True, exist_ok=True)
        (folder / "images" / name).write_bytes(data)
        saved[src] = f"images/{name}"
        img.attrs = {"src": saved[src], "alt": img.attrs.get("alt", ""), "data-local": "1"}
    return count


MAX_SIDE = 2000      # px; magazine-page scans stay readable at this size
WEBP_QUALITY = 80


def compress_images(body, folder):
    """Shrink and convert saved images to WebP (needs Pillow; skipped without it).
    Records width/height so pages don't jump while images load."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        print("    (install Pillow to compress images: pip install Pillow)", file=sys.stderr)
        return
    done = {}
    for img in [n for n in body.iter() if n.tag == "img" and n.attrs.get("data-local")]:
        src = img.attrs["src"]
        if src in done:
            img.attrs.update(done[src])
            continue
        path = folder / src
        try:
            with Image.open(path) as im:
                animated = getattr(im, "is_animated", False)
                if path.suffix.lower() == ".svg" or animated:
                    done[src] = {}
                    continue
                if path.suffix.lower() == ".webp" and max(im.size) <= MAX_SIDE:
                    done[src] = {"width": str(im.size[0]), "height": str(im.size[1])}
                    img.attrs.update(done[src])
                    continue
                im = ImageOps.exif_transpose(im)
                if max(im.size) > MAX_SIDE:
                    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
                if im.mode not in ("RGB", "RGBA", "L", "LA"):
                    im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                out = path.with_suffix(".webp")
                im.save(out, "WEBP", quality=WEBP_QUALITY, method=6)
                size = im.size
        except Exception as e:  # noqa: BLE001  (unreadable/odd files: keep as they are)
            print(f"    could not compress {path.name}: {e}", file=sys.stderr)
            done[src] = {}
            continue
        if out != path:
            path.unlink(missing_ok=True)
        new = {"src": f"images/{out.name}", "width": str(size[0]), "height": str(size[1])}
        img.attrs.update(new)
        done[src] = new


def remove_unused_images(folder, body):
    used = {n.attrs["src"] for n in body.iter() if n.tag == "img" and n.attrs.get("data-local")}
    if (folder / "images").exists():
        for f in (folder / "images").iterdir():
            if f"images/{f.name}" not in used:
                f.unlink()
        if not any((folder / "images").iterdir()):
            (folder / "images").rmdir()


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
            out.append(html.escape(c.translate(INVISIBLE), quote=False))
            continue
        if c.tag in ("#drop", "h1") or is_noise(c):
            continue
        if c.tag == "figure" and not any(x.tag == "img" and x.attrs.get("data-local")
                                         for x in c.iter()):
            cap = c.find("figcaption")
            if cap and to_html(cap, base).strip():
                out.append(f'<p class="archive-caption">{to_html(cap, base)}</p>')
            continue
        if c.tag not in KEEP:  # unwrap div/span/section/etc.
            out.append(to_html(c, base))
            continue
        attrs = ""
        if c.tag == "a":
            href = safe_link(c.attrs.get("href"), base)
            if href and not href.startswith("javascript:"):
                attrs = f' href="{html.escape(href)}"'
        elif c.tag == "img":
            if not c.attrs.get("data-local"):
                continue  # never hotlink: only locally saved images are shown
            attrs = (f' src="{html.escape(c.attrs["src"])}"'
                     f' alt="{html.escape(c.attrs.get("alt", ""))}"')
            if c.attrs.get("width"):
                attrs += f' width="{c.attrs["width"]}" height="{c.attrs["height"]}"'
            attrs += ' loading="lazy"'
            if not any(a.tag == "a" for a in ancestors(c)):
                # no JavaScript lightbox: clicking opens the full image
                out.append(f'<a class="archive-zoom" href="{html.escape(c.attrs["src"])}"><img{attrs}></a>')
                continue
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
    markup = re.sub(r"(?:\s*<br>)+\s*(</(?:p|li|h[2-6]|figcaption|blockquote)>)", r"\1", markup)
    markup = re.sub(r"(<(?:p|li)(?: [^>]*)?>)\s*(?:<br>\s*)+", r"\1", markup)
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

    def sources():
        if not is_dead(url):
            yield url, url, None  # healthy sites: live page first, archive only if that fails
        caps = captures(url, wayback)
        if not caps and wayback == "https://web.archive.org":
            caps = available_captures(url)
        for ts, original in caps[:8]:
            yield (f"{wayback}/web/{ts}id_/{original}",
                   f"https://web.archive.org/web/{ts}/{original}", ts)

    global IMG_PREFIX
    rejected = 0
    for raw_url, cite_url, ts in sources():
        IMG_PREFIX = f"{wayback}/web/{ts}im_/" if ts else ""
        try:
            # archived copies get full retries; the live page is tried once
            body, ctype = fetch(raw_url, retries=4 if ts else 1)
        except Exception as e:  # noqa: BLE001
            print(f"    fetch failed ({raw_url}): {e}", file=sys.stderr)
            continue
        if "html" not in (ctype or "html"):
            continue
        root = parse(decode(body, ctype))
        node = main_content(root)
        text = to_markdown_node(node, url)
        if NOT_AN_ARTICLE.search(text) or len(text) < 400:
            rejected += 1
            where = f"capture {ts}" if ts else "live page"
            print(f"    skipping {where}: not the article "
                  f"({'blocked/parked domain page' if NOT_AN_ARTICLE.search(text) else 'too little text'})",
                  file=sys.stderr)
            continue
        if len(text) < 1500:
            print(f"    warning: only {len(text)} characters of text, check this one", file=sys.stderr)
        return {"status": "ok", "page_title": page_title(root), "node": node, "ts": ts,
                "source": cite_url,
                "captured": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if ts else "live page"}
    why = "only blocked/placeholder copies found" if rejected else "no archived copy found"
    return {"status": "missing", "note": f"Not recovered ({why}): <{url}>"}


def load_saved(item, outdir):
    """Read back an article page written by an earlier run (for --rebuild)."""
    url = item["url"]
    if url.lower().endswith(".pdf"):
        return {"status": "pdf", "note": f"PDF, not copied: <{url}>"}
    if any(h in urllib.parse.urlparse(url).netloc for h in SKIP_HOSTS):
        return {"status": "skipped", "note": f"Publisher/repository page, not copied: <{url}>"}
    page = outdir / slug_for(url) / "index.html"
    if not page.exists():
        return {"status": "missing", "note": f"Not recovered yet: <{url}>"}
    root = parse(page.read_text(encoding="utf-8"))
    art = root.find("article")
    art.children = [c for c in art.children if not (isinstance(c, Node) and c.tag == "header")]
    for n in art.iter():
        if n.tag == "a" and "archive-zoom" in n.cls():
            n.tag, n.attrs = "span", {}  # re-added on output
        if n.tag == "img" and n.attrs.get("src", "").startswith("images/"):
            n.attrs["data-local"] = "1"
        if n.tag == "p" and "archive-caption" in n.cls():
            n.attrs = {}
    note = root.find("aside")
    link = note.find("a") if note else None
    m = re.search(r"web\.archive\.org/web/(\d{14})/", link.attrs.get("href", "")) if link else None
    ts = m.group(1) if m else None
    return {"status": "ok", "node": art, "ts": ts,
            "source": link.attrs["href"] if m else url,
            "captured": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if ts else "live page"}


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


def link_for(item, res):
    """Where the site should link: the local copy if it came from the archive,
    otherwise the original (or its Wayback copy, for dead domains)."""
    if res["status"] == "ok" and res["ts"]:
        return f"archive/{res['slug']}/"
    if is_dead(item["url"]):
        return f"https://web.archive.org/web/2022/{item['url']}"
    return item["url"]


def update_index(index_path, results):
    """Point the article links in index.html at the right place."""
    text = index_path.read_text(encoding="utf-8")
    targets = {item["url"]: link_for(item, res) for item, res in results
               if res["status"] in ("ok", "missing")}
    changed = 0

    def relink(m):
        nonlocal changed
        tag = m.group(0)
        href = re.search(r'href="([^"]*)"', tag)
        orig = re.search(r'data-original="([^"]*)"', tag)
        url = html.unescape(orig.group(1)) if orig else unwrap(html.unescape(href.group(1) if href else ""))
        if url not in targets:
            return tag
        new = f'<a class="pub" href="{html.escape(targets[url])}" data-original="{html.escape(url)}">'
        changed += new != tag
        return new

    text = re.sub(r'<a class="pub"[^>]*>', relink, text)
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
    ap.add_argument("--rebuild", action="store_true",
                    help="no downloading: re-clean and re-compress the pages already in archive/")
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
        res = load_saved(item, outdir) if args.rebuild else revive(item, wayback)
        if res["status"] == "ok":
            res["slug"] = slug_for(item["url"])
            folder = outdir / res["slug"]
            clean_article(res["node"], item["url"])
            if not args.rebuild:
                if (folder / "images").exists():
                    for old in (folder / "images").iterdir():
                        old.unlink()
                save_images(res["node"], item["url"], folder, wayback, res["ts"])
            compress_images(res["node"], folder)
            remove_unused_images(folder, res["node"])
            imgs = len({x.attrs["src"] for x in res["node"].iter()
                        if x.tag == "img" and x.attrs.get("data-local")})
            content = pretty(to_html(res["node"], item["url"]))
            content = "\n".join("      " + ln for ln in content.splitlines())
            write_article_page(item, res, folder, content)
            MD_IMG_BASE = f"{outdir.name}/{res['slug']}/"
            res["markdown"] = to_markdown_node(res["node"], item["url"])
            print(f"    -> ok ({res['captured']}, {imgs} images) -> {folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder}/")
        else:
            print(f"    -> {res['status']}")
            stale = outdir / slug_for(item["url"])
            if res["status"] == "missing" and not args.rebuild and (stale / "index.html").exists():
                shutil.rmtree(stale)  # remove a bad copy from an earlier run
                print(f"    removed old copy {stale.name}/")
        results.append((item, res))
        if n < len(items) and not args.rebuild:
            time.sleep(args.pause)

    ok = [r for _, r in results if r["status"] == "ok"]
    if ok and not args.only:
        write_archive_index(results, outdir)
    if args.only:
        print(f"\n--only: wrote the page(s) above; {Path(args.output).name}, archive/index.html "
              f"and index.html were left alone.")
        return
    Path(args.output).write_text(render_markdown(results), encoding="utf-8")
    print(f"\nWrote {args.output}: {len(ok)}/{len(results)} articles recovered.")
    if ok and not args.no_update_index and not args.only:
        print(f"Updated {update_index(index_path, results)} links in {index_path.name}.")


if __name__ == "__main__":
    main()
