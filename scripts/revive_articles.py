#!/usr/bin/env python3
"""Revive the articles linked from index.html into a single Markdown file.

For every article link on the site (links with class="pub"), the script:
  1. unwraps Wayback Machine links back to the original URL,
  2. asks the Wayback CDX API for the most recent good capture,
  3. downloads the raw archived page (falls back to the live URL),
  4. extracts the article body and converts it to Markdown,
  5. writes everything to articles.md in the site root.

PDFs and paywalled publisher pages are listed with their links, not copied.

Standard library only. Usage:
    python3 scripts/revive_articles.py              # writes articles.md
    python3 scripts/revive_articles.py -o out.md
    python3 scripts/revive_articles.py --only bnieuws
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
BOILERPLATE = re.compile(r"(nav|menu|footer|header|share|social|cookie|related|"
                         r"comment|sidebar|newsletter|subscribe|breadcrumb)", re.I)


def is_noise(n):
    if n.tag in DROP:
        return True
    marker = " ".join(n.cls()) + " " + (n.attrs.get("id") or "")
    return bool(BOILERPLATE.search(marker)) and n.tag not in ("body", "main", "article")


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


def image_url(src, base):
    src = absolute(src, base)
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
            src = image_url(c.attrs.get("src"), base)
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
            if img and img.attrs.get("src"):
                alt = squash(cap.text()) if cap else img.attrs.get("alt", "")
                blocks.append(f"![{alt}]({image_url(img.attrs['src'], base)})")
                if cap and squash(cap.text()):
                    blocks.append(f"*{squash(cap.text())}*")
        elif t == "img":
            flush()
            if c.attrs.get("src"):
                blocks.append(f"![{c.attrs.get('alt', '')}]({image_url(c.attrs['src'], base)})")
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


def to_markdown(markup, base):
    root = parse(markup)
    title = page_title(root)
    body = main_content(root)
    md = block_md(body, base)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return title, md


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def revive(item, wayback):
    url = item["url"]
    host = urllib.parse.urlparse(url).netloc
    if url.lower().endswith(".pdf"):
        return {"status": "pdf", "note": f"PDF, not copied: <{url}>"}
    if any(h in host for h in SKIP_HOSTS):
        return {"status": "skipped", "note": f"Publisher/repository page, not copied: <{url}>"}

    cap = latest_capture(url, wayback)
    sources = []
    if cap:
        ts, original = cap
        sources.append((f"{wayback}/web/{ts}id_/{original}",
                        f"{wayback}/web/{ts}/{original}", ts))
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
        title, md = to_markdown(decode(body, ctype), url)
        if len(md) < 200:
            print(f"    too little text extracted from {raw_url}", file=sys.stderr)
            continue
        captured = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if ts else "live page"
        return {"status": "ok", "title": title, "markdown": md,
                "source": cite_url, "captured": captured}
    return {"status": "missing", "note": f"No archived copy found for <{url}>"}


def render(results):
    out = ["# Articles", "",
           "Copies of the articles linked from the site, recovered from the "
           "[Wayback Machine](https://web.archive.org/) where the originals are offline.",
           "", "## Contents", ""]
    for item, _ in results:
        slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")
        out.append(f"- [{item['title']}](#{slug})")
    out.append("")
    for item, res in results:
        slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")
        out += ["---", "", f'<a id="{slug}"></a>', "", f"### {item['title']}", ""]
        meta = [x for x in (item["date"], f"in *{item['venue']}*" if item["venue"] else "") if x]
        if meta:
            out += [" · ".join(meta), ""]
        if res["status"] == "ok":
            out += [f"Original: <{item['url']}>  ",
                    f"Archived copy: <{res['source']}> ({res['captured']})", "",
                    res["markdown"], ""]
        else:
            out += [f"_{res['note']}_", ""]
    return "\n".join(out).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--index", default=str(ROOT / "index.html"))
    ap.add_argument("-o", "--output", default=str(ROOT / "articles.md"))
    ap.add_argument("--only", help="only process URLs containing this text")
    ap.add_argument("--wayback", default="https://web.archive.org",
                    help="Wayback Machine base URL (for testing)")
    ap.add_argument("--pause", type=float, default=1.5,
                    help="seconds to wait between articles (be polite to archive.org)")
    args = ap.parse_args()

    items = collect_articles(Path(args.index))
    if args.only:
        items = [i for i in items if args.only in i["url"]]
    if not items:
        sys.exit("No article links found.")

    results = []
    for n, item in enumerate(items, 1):
        print(f"[{n}/{len(items)}] {item['title']}\n    {item['url']}")
        res = revive(item, args.wayback.rstrip("/"))
        print(f"    -> {res['status']}" + (f" ({res['captured']})" if res["status"] == "ok" else ""))
        results.append((item, res))
        if n < len(items):
            time.sleep(args.pause)

    Path(args.output).write_text(render(results), encoding="utf-8")
    ok = sum(1 for _, r in results if r["status"] == "ok")
    print(f"\nWrote {args.output}: {ok}/{len(results)} articles recovered.")


if __name__ == "__main__":
    main()
