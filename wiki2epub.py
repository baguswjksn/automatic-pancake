#!/usr/bin/env python3
"""
wiki2epub.py - Convert Wikipedia articles into EPUB files optimised for the
Xteink X4 e-reader (4.3" e-ink, 480x800 px, grayscale, low RAM).

What it does for the X4:
  * splits the article into one XHTML file per section (small files = less RAM
    and faster page turns on the device),
  * converts every image to grayscale baseline JPEG, resized to fit 480x800
    (progressive JPEG / SVG / WebP are not reliably supported),
  * flattens infoboxes into plain paragraphs (wide tables do not fit),
  * strips navboxes, edit links, hatnote clutter, styles and scripts,
  * removes links to other wiki pages (they cannot be opened on the device),
  * writes EPUB 3 with an EPUB 2 NCX fallback and a generated cover.

Usage:
  python wiki2epub.py https://id.wikipedia.org/wiki/Borobudur
  python wiki2epub.py "Alan Turing" --lang en -o turing.epub
  python wiki2epub.py URL1 URL2 URL3 --outdir books/
  python wiki2epub.py URL --no-images --no-references
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import re
import sys
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --- Xteink X4 display profile ------------------------------------------------
SCREEN_W, SCREEN_H = 480, 800
IMG_MAX_W, IMG_MAX_H = 440, 640  # leave room for margins and caption
IMG_MIN_SIDE = 48                # skip icons / flags smaller than this
JPEG_QUALITY = 70
THUMB_WIDTH = 500                # Wikimedia standard thumbnail bucket

USER_AGENT = "wiki2epub-xteink/1.0 (personal e-reader conversion)"

REMOVE_SELECTORS = [
    "script", "style", "link", "meta", "noscript",
    ".mw-editsection", ".navbox", ".navbox-styles", ".vertical-navbox",
    ".sidebar", ".metadata", ".ambox", ".ombox", ".tmbox", ".cmbox",
    ".noprint", ".mw-empty-elt", ".portalbox", ".portal", ".sistersitebox",
    ".mbox-small", ".shortdescription", ".mw-jump-link", ".toc", "#toc",
    ".authority-control", ".catlinks", ".mw-cite-backlink",
    ".mw-indicators", ".plainlinks.hlist", "audio", "video", "iframe",
    ".mw-kartographer-maplink", ".geo-default", ".coordinates", "#coordinates",
]
REFERENCE_SELECTORS = [".reflist", ".references", "ol.references", ".refbegin"]

ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "b", "strong", "i",
    "em", "u", "s", "sub", "sup", "small", "code", "pre", "kbd", "var",
    "blockquote", "q", "cite", "abbr", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "img", "figure", "figcaption", "a", "span", "div",
}
ALLOWED_ATTRS = {
    "a": {"href", "id"}, "img": {"src", "alt"}, "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"}, "ol": {"start"}, "li": {"value"},
}
KEEP_ID_TAGS = {"h2", "h3", "h4", "h5", "h6", "li", "p", "span", "div", "a",
                "table", "figure"}

CSS = """\
body { margin: 0; padding: 0; line-height: 1.35; }
h1 { font-size: 1.5em; margin: 0.4em 0 0.6em 0; text-align: center; }
h2 { font-size: 1.3em; margin: 0.6em 0 0.4em 0; border-bottom: 1px solid #000; }
h3 { font-size: 1.15em; margin: 0.8em 0 0.3em 0; }
h4, h5, h6 { font-size: 1em; margin: 0.6em 0 0.2em 0; }
p { margin: 0 0 0.6em 0; text-indent: 0; }
figure { margin: 0.8em 0; text-align: center; page-break-inside: avoid; }
figure img, img.block { max-width: 100%; height: auto; }
figcaption { font-size: 0.85em; font-style: italic; margin-top: 0.2em; }
blockquote { margin: 0.5em 1em; font-style: italic; }
table { border-collapse: collapse; font-size: 0.85em; margin: 0.6em 0; }
th, td { border: 1px solid #000; padding: 2px 4px; vertical-align: top; }
.infobox { border: 1px solid #000; padding: 0.4em; margin: 0 0 1em 0; font-size: 0.9em; }
.infobox p { margin: 0 0 0.3em 0; }
.infobox-title { font-weight: bold; text-align: center; font-size: 1.1em; }
.meta { font-size: 0.8em; text-align: center; margin-top: 2em; }
ol.refs, .refs li { font-size: 0.8em; }
"""


# --- data ---------------------------------------------------------------------
@dataclass
class Chapter:
    title: str
    filename: str
    nodes: list = field(default_factory=list)
    html: str = ""


@dataclass
class Article:
    title: str
    lang: str
    url: str
    revid: int | None
    html: str


# --- fetching -----------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def parse_target(target: str, default_lang: str) -> tuple[str, str]:
    """Return (lang, title) from a Wikipedia URL or a plain title."""
    if target.startswith(("http://", "https://")):
        u = urlparse(target)
        sub = u.netloc.split(".")[0]  # id.wikipedia.org / id.m.wikipedia.org
        lang = default_lang if sub in ("www", "m") else sub
        if u.path.startswith("/wiki/"):
            title = unquote(u.path[len("/wiki/"):])
        else:
            title = parse_qs(u.query).get("title", [""])[0]
            if not title:
                raise ValueError(f"Cannot find article title in URL: {target}")
        return lang, title.replace("_", " ")
    return default_lang, target


def fetch_article(session: requests.Session, lang: str, title: str) -> Article:
    api = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "parse", "page": title, "format": "json",
        "formatversion": 2, "redirects": 1, "prop": "text|displaytitle|revid",
        "disableeditsection": 1, "disabletoc": 1,
    }
    r = session.get(api, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Wikipedia API error: {data['error'].get('info')}")
    p = data["parse"]
    clean_title = BeautifulSoup(p.get("displaytitle", p["title"]), "html.parser").get_text()
    url = f"https://{lang}.wikipedia.org/wiki/{p['title'].replace(' ', '_')}"
    return Article(clean_title, lang, url, p.get("revid"), p["text"])


# --- images -------------------------------------------------------------------
def upgrade_thumb_url(src: str) -> str:
    """Ask Wikimedia for a thumbnail that matches the X4 screen width."""
    return re.sub(r"/(\d+)px-", f"/{THUMB_WIDTH}px-", src, count=1) if "/thumb/" in src else src


def to_x4_jpeg(raw: bytes) -> bytes | None:
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
    if min(im.size) < IMG_MIN_SIDE:
        return None
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    im = ImageOps.grayscale(im)
    im.thumbnail((IMG_MAX_W, IMG_MAX_H), Image.LANCZOS)
    im = ImageOps.autocontrast(im, cutoff=1)
    out = io.BytesIO()
    # baseline (non-progressive) JPEG decodes on the X4's microcontroller
    im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=False)
    return out.getvalue()


class ImageStore:
    def __init__(self, session: requests.Session, enabled: bool):
        self.session = session
        self.enabled = enabled
        self.files: dict[str, bytes] = {}   # epub path -> data
        self.cache: dict[str, str | None] = {}

    def get(self, src: str) -> str | None:
        """Download+convert an image, return its path inside the EPUB."""
        if not self.enabled or not src:
            return None
        src = urljoin("https://", src) if src.startswith("//") else src
        if src in self.cache:
            return self.cache[src]
        data = None
        for url in dict.fromkeys([upgrade_thumb_url(src), src]):
            try:
                r = self.session.get(url, timeout=30)
                if r.ok:
                    data = to_x4_jpeg(r.content)
                    if data:
                        break
            except requests.RequestException:
                continue
        path = None
        if data:
            path = f"images/img{len(self.files) + 1:03d}.jpg"
            self.files[path] = data
        self.cache[src] = path
        return path


# --- cleaning -----------------------------------------------------------------
def flatten_infobox(soup: BeautifulSoup, table: Tag) -> Tag:
    box = soup.new_tag("div", attrs={"class": "infobox"})
    for tr in table.find_all("tr"):
        th = tr.find("th", recursive=False)
        tds = tr.find_all("td", recursive=False)
        if th and tds:
            p = soup.new_tag("p")
            b = soup.new_tag("b")
            b.string = th.get_text(" ", strip=True) + ": "
            p.append(b)
            for td in tds:
                for c in list(td.contents):
                    p.append(c.extract())
            box.append(p)
        elif th:
            p = soup.new_tag("p", attrs={"class": "infobox-title"})
            p.string = th.get_text(" ", strip=True)
            box.append(p)
        else:
            for td in tds:
                div = soup.new_tag("div")
                for c in list(td.contents):
                    div.append(c.extract())
                box.append(div)
    caption = table.find("caption")
    if caption:
        p = soup.new_tag("p", attrs={"class": "infobox-title"})
        p.string = caption.get_text(" ", strip=True)
        box.insert(0, p)
    table.replace_with(box)
    return box


def replace_math(soup: BeautifulSoup) -> None:
    for el in soup.select(".mwe-math-element"):
        ann = el.find("annotation")
        img = el.find("img")
        tex = (ann.get_text() if ann else (img.get("alt") if img else el.get_text())).strip()
        code = soup.new_tag("code")
        code.string = tex
        el.replace_with(code)


def normalize_figures(soup: BeautifulSoup) -> None:
    """Turn the various Wikipedia image markups into <figure><img/><figcaption/>."""
    for thumb in soup.select("div.thumb"):  # legacy markup
        img = thumb.find("img")
        cap = thumb.select_one(".thumbcaption")
        fig = soup.new_tag("figure")
        if img:
            fig.append(img.extract())
        if cap:
            fc = soup.new_tag("figcaption")
            fc.extend(list(cap.contents))
            fig.append(fc)
        thumb.replace_with(fig)
    for gallery in soup.select("ul.gallery"):
        wrapper = soup.new_tag("div")
        for box in gallery.select("li.gallerybox"):
            img = box.find("img")
            if not img:
                continue
            fig = soup.new_tag("figure")
            fig.append(img.extract())
            text = box.select_one(".gallerytext")
            if text and text.get_text(strip=True):
                fc = soup.new_tag("figcaption")
                fc.extend(list(text.contents))
                fig.append(fc)
            wrapper.append(fig)
        gallery.replace_with(wrapper)


def clean_html(raw: str, opts: argparse.Namespace) -> BeautifulSoup:
    soup = BeautifulSoup(raw, "lxml")
    root = soup.select_one(".mw-parser-output") or soup.body or soup

    for sel in REMOVE_SELECTORS:
        for el in root.select(sel):
            el.decompose()
    if not opts.citations:
        for el in root.select("sup.reference, sup.noprint, .mw-ref"):
            el.decompose()
    if opts.no_references:
        for sel in REFERENCE_SELECTORS:
            for el in root.select(sel):
                el.decompose()
    else:
        for ol in root.select("ol.references"):
            ol["class"] = "refs"

    replace_math(root)
    for table in root.select("table.infobox, table.infobox_v2, table.infobox_v3"):
        flatten_infobox(soup, table)
    normalize_figures(root)

    # unwrap wiki links (not usable offline); keep in-page anchors
    for a in root.find_all("a"):
        href = a.get("href", "")
        if not href.startswith("#"):
            a.unwrap()

    # drop disallowed tags (keep their text) and attributes
    for el in root.find_all(True):
        if el.name not in ALLOWED_TAGS:
            el.unwrap()
            continue
        keep = set(ALLOWED_ATTRS.get(el.name, ()))
        if el.name in KEEP_ID_TAGS:
            keep.add("id")
        if el.name in ("div", "ol") and el.get("class"):
            cls = el.get("class")
            cls = cls if isinstance(cls, list) else cls.split()
            wanted = [c for c in cls if c in ("infobox", "infobox-title", "refs")]
            el.attrs = {k: v for k, v in el.attrs.items() if k in keep}
            if wanted:
                el["class"] = " ".join(wanted)
            continue
        if el.name == "p" and "infobox-title" in (el.get("class") or []):
            el.attrs = {"class": "infobox-title"}
            continue
        el.attrs = {k: v for k, v in el.attrs.items() if k in keep}

    # empty paragraphs / spans
    for el in root.find_all(["p", "span", "div"]):
        if not el.get_text(strip=True) and not el.find("img") and not el.get("id"):
            el.decompose()
    return soup


# --- splitting into chapters --------------------------------------------------
def heading_of(node) -> Tag | None:
    if not isinstance(node, Tag):
        return None
    if node.name == "h2":
        return node
    if node.name == "div" and node.find("h2", recursive=False) and \
            len([c for c in node.children if isinstance(c, Tag)]) == 1:
        return node.find("h2")
    return None


def split_chapters(soup: BeautifulSoup, title: str) -> list[Chapter]:
    root = soup.select_one(".mw-parser-output") or soup.body or soup
    # unwrap single wrapper divs so sections are top-level siblings
    nodes = list(root.children)
    while len([n for n in nodes if isinstance(n, Tag)]) == 1 and \
            next(n for n in nodes if isinstance(n, Tag)).name == "div" and \
            not heading_of(next(n for n in nodes if isinstance(n, Tag))):
        nodes = list(next(n for n in nodes if isinstance(n, Tag)).children)

    chapters = [Chapter(title, "chap000.xhtml")]
    for node in nodes:
        h2 = heading_of(node)
        if h2 is not None:
            name = h2.get_text(" ", strip=True)
            chapters.append(Chapter(name, f"chap{len(chapters):03d}.xhtml",
                                    [h2.extract()]))
            continue
        if isinstance(node, NavigableString) and not node.strip():
            continue
        chapters[-1].nodes.append(node)

    # drop sections that ended up empty (e.g. "External links" after cleaning)
    result = [chapters[0]]
    for ch in chapters[1:]:
        body = ch.nodes[1:]  # skip the heading itself
        text = "".join(n.get_text() if isinstance(n, Tag) else str(n) for n in body)
        has_img = any(isinstance(n, Tag) and (n.name == "img" or n.find("img")) for n in body)
        if text.strip() or has_img:
            result.append(ch)
    for i, ch in enumerate(result):
        ch.filename = f"chap{i:03d}.xhtml"
    return result


# --- EPUB writing -------------------------------------------------------------
def xhtml_page(title: str, body: str, lang: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{lang}" lang="{lang}">\n<head>\n<meta charset="utf-8"/>\n'
        f"<title>{html.escape(title)}</title>\n"
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def render_chapters(chapters: list[Chapter], article: Article,
                    images: ImageStore) -> None:
    # map element ids -> chapter so in-page links still work after splitting
    id_map: dict[str, str] = {}
    for ch in chapters:
        for n in ch.nodes:
            if isinstance(n, Tag):
                for el in [n, *n.find_all(id=True)]:
                    if el.get("id"):
                        id_map.setdefault(el["id"], ch.filename)

    for ci, ch in enumerate(chapters):
        frag = BeautifulSoup("", "lxml")
        container = frag.new_tag("div")
        if ci == 0:
            h1 = frag.new_tag("h1")
            h1.string = article.title
            container.append(h1)
        for n in ch.nodes:
            container.append(n)

        for img in container.find_all("img"):
            path = images.get(img.get("src", ""))
            if not path:
                parent = img.parent
                img.decompose()
                if parent is not None and parent.name == "figure" and not parent.find("img"):
                    parent.decompose()
                continue
            img["src"] = path
            img["alt"] = img.get("alt") or ""
            if img.parent is not None and img.parent.name not in ("figure", "div"):
                img["class"] = "block"

        for a in container.find_all("a"):
            target = a.get("href", "")[1:]
            if target in id_map:
                a["href"] = f"{id_map[target]}#{target}" if id_map[target] != ch.filename else f"#{target}"
            else:
                a.unwrap()

        if ci == len(chapters) - 1:
            meta = frag.new_tag("p", attrs={"class": "meta"})
            meta.string = (f"Source: {article.url} (revision {article.revid}). "
                           "Text available under CC BY-SA 4.0.")
            container.append(meta)

        body = "".join(str(c) for c in container.contents)
        ch.html = xhtml_page(ch.title, body, article.lang)


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_cover(title: str, lang: str) -> bytes:
    im = Image.new("L", (SCREEN_W, SCREEN_H), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([20, 20, SCREEN_W - 21, SCREEN_H - 21], outline=0, width=4)
    d.rectangle([32, 32, SCREEN_W - 33, SCREEN_H - 33], outline=0, width=1)

    size = 52
    while True:
        font = _font(size)
        lines, line = [], ""
        for word in title.split():
            trial = f"{line} {word}".strip()
            if d.textlength(trial, font=font) <= SCREEN_W - 100 or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
        if (len(lines) <= 6 and all(d.textlength(l, font=font) <= SCREEN_W - 100 for l in lines)) \
                or size <= 20:
            break
        size -= 4

    lh = int(size * 1.25)
    y = (SCREEN_H - lh * len(lines)) // 2 - 40
    for l in lines:
        w = d.textlength(l, font=font)
        d.text(((SCREEN_W - w) / 2, y), l, font=font, fill=0)
        y += lh
    d.line([140, y + 20, SCREEN_W - 140, y + 20], fill=0, width=2)
    small = _font(22)
    label = f"Wikipedia ({lang})"
    d.text(((SCREEN_W - d.textlength(label, font=small)) / 2, y + 40), label, font=small, fill=0)

    out = io.BytesIO()
    im.save(out, "JPEG", quality=85, progressive=False)
    return out.getvalue()


def write_epub(path: Path, article: Article, chapters: list[Chapter],
               images: ImageStore, cover: bool) -> None:
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, article.url)}"
    modified = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    esc = html.escape

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []
    if cover:
        manifest.append('<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
        manifest.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="cover"/>')
    for i, ch in enumerate(chapters):
        manifest.append(f'<item id="c{i}" href="{ch.filename}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    for i, img_path in enumerate(images.files):
        manifest.append(f'<item id="img{i}" href="{img_path}" media-type="image/jpeg"/>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{article.lang}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{esc(article.title)}</dc:title>
    <dc:language>{article.lang}</dc:language>
    <dc:creator>Wikipedia contributors</dc:creator>
    <dc:publisher>Wikipedia</dc:publisher>
    <dc:source>{esc(article.url)}</dc:source>
    <dc:rights>CC BY-SA 4.0</dc:rights>
    <meta property="dcterms:modified">{modified}</meta>
    {'<meta name="cover" content="cover-image"/>' if cover else ''}
  </metadata>
  <manifest>
    {chr(10).join('    ' + m for m in manifest).lstrip()}
  </manifest>
  <spine toc="ncx">
    {chr(10).join('    ' + s for s in spine).lstrip()}
  </spine>
</package>
"""

    nav_items = "\n".join(f'      <li><a href="{c.filename}">{esc(c.title)}</a></li>' for c in chapters)
    nav = xhtml_page("Daftar Isi" if article.lang == "id" else "Contents",
                     f'<nav epub:type="toc" id="toc">\n  <h1>{esc(article.title)}</h1>\n'
                     f"  <ol>\n{nav_items}\n  </ol>\n</nav>", article.lang)

    nav_points = "\n".join(
        f'    <navPoint id="np{i}" playOrder="{i + 1}"><navLabel><text>{esc(c.title)}</text></navLabel>'
        f'<content src="{c.filename}"/></navPoint>' for i, c in enumerate(chapters))
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{book_id}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{esc(article.title)}</text></docTitle>
  <navMap>
{nav_points}
  </navMap>
</ncx>
"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    with zipfile.ZipFile(path, "w") as z:
        # mimetype must be first and uncompressed
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        def w(name, data):
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        w("META-INF/container.xml", container)
        w("OEBPS/content.opf", opf)
        w("OEBPS/nav.xhtml", nav)
        w("OEBPS/toc.ncx", ncx)
        w("OEBPS/style.css", CSS)
        if cover:
            w("OEBPS/images/cover.jpg", make_cover(article.title, article.lang))
            w("OEBPS/cover.xhtml", xhtml_page(
                article.title,
                f'<div style="text-align:center"><img src="images/cover.jpg" alt="{esc(article.title)}" '
                'style="max-width:100%;height:auto"/></div>', article.lang))
        for ch in chapters:
            w(f"OEBPS/{ch.filename}", ch.html)
        for img_path, data in images.files.items():
            w(f"OEBPS/{img_path}", data)


# --- main ---------------------------------------------------------------------
def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "", name).strip().replace(" ", "_")
    return name[:80] or "wikipedia"


def convert(article: Article, opts: argparse.Namespace, session: requests.Session,
            out_path: Path) -> Path:
    soup = clean_html(article.html, opts)
    chapters = split_chapters(soup, article.title)
    images = ImageStore(session, enabled=not opts.no_images)
    render_chapters(chapters, article, images)
    write_epub(out_path, article, chapters, images, cover=not opts.no_cover)
    print(f"  {len(chapters)} chapters, {len(images.files)} images -> {out_path} "
          f"({out_path.stat().st_size / 1024:.0f} KB)")
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Convert Wikipedia articles to EPUB for the Xteink X4.")
    ap.add_argument("targets", nargs="+", help="Wikipedia URL(s) or article title(s)")
    ap.add_argument("--lang", default="id", help="language for plain titles (default: id)")
    ap.add_argument("-o", "--output", help="output file (only with a single article)")
    ap.add_argument("--outdir", default=".", help="output directory (default: current)")
    ap.add_argument("--no-images", action="store_true", help="do not include images")
    ap.add_argument("--no-cover", action="store_true", help="do not generate a cover")
    ap.add_argument("--no-references", action="store_true", help="drop the reference list")
    ap.add_argument("--citations", action="store_true", help="keep [1] citation markers in text")
    opts = ap.parse_args(argv)

    if opts.output and len(opts.targets) > 1:
        ap.error("-o/--output can only be used with a single article; use --outdir")

    session = make_session()
    outdir = Path(opts.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for target in opts.targets:
        try:
            lang, title = parse_target(target, opts.lang)
            print(f"Fetching [{lang}] {title} ...")
            article = fetch_article(session, lang, title)
            out = Path(opts.output) if opts.output else outdir / f"{safe_filename(article.title)}.epub"
            convert(article, opts, session, out)
        except Exception as e:  # keep going with the other articles
            failures += 1
            print(f"  ERROR: {e}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
