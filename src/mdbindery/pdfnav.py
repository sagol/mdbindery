"""PDF navigation and metadata: anchors and locators in the print HTML, then the outline, Info and XMP written into
Chrome's PDF, and the gates that check them in the final file.

The outline holds exactly pandoc's contents (nav#TOC). Each entry's link points to a generated anchor inside its
target, so its destination never depends on the heading's own box (a display: contents heading has none). Locators
(an empty span and a hidden link to it, which adds no annotation, tag or text) find the title page, the rights line and
the first contents page in the final PDF, independent of the outline."""
import html
import re
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from xml.sax.saxutils import escape

from .build import BuildError

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
TITLE, RIGHTS, CONTENTS = 'mdb-title-page', 'mdb-rights', 'mdb-contents-page'
FIT_ARGS = {'/XYZ': ('/Left', '/Top', '/Zoom'), '/Fit': (), '/FitB': (), '/FitH': ('/Top',), '/FitBH': ('/Top',),
            '/FitV': ('/Left',), '/FitBV': ('/Left',), '/FitR': ('/Left', '/Bottom', '/Right', '/Top')}


class _Scan(HTMLParser):
    """Source positions in pandoc's print HTML: each id with its element and the end of its start tag, the contents
    links with their nesting, and the title block's start and closing tags. Regions an anchor may not target: the
    cover, the title block and the contents nav."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.starts = [0]
        for line in text.split('\n')[:-1]:
            self.starts.append(self.starts[-1] + len(line) + 1)
        self.stack, self.ids, self.links = [], {}, []
        self.nav = self.header = self.header_close = None
        self._nav_at = self._header_at = None

    def _offset(self):
        line, col = self.getpos()
        return self.starts[line - 1] + col

    def _region(self):
        return any(tag == 'section' and 'pdf-cover' in classes or ident in ('title-block-header', 'TOC')
                   for tag, ident, classes in self.stack)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        start = self._offset()
        end = start + len(self.get_starttag_text())
        ident = attrs.get('id')
        if ident is not None and ident not in self.ids:
            self.ids[ident] = {'tag': tag, 'end': end, 'region': self._region()}
        if tag == 'nav' and ident == 'TOC' and self.nav is None:
            self.nav, self._nav_at = end, len(self.stack)
        elif self._nav_at is not None and self._nav_at >= 0 and tag == 'a' and (attrs.get('href') or '').startswith('#'):
            depth = sum(1 for t, _, _ in self.stack[self._nav_at:] if t == 'ul')
            self.links.append({'depth': depth, 'start': start, 'end': end, 'raw': self.get_starttag_text(), 'href': attrs['href']})
        if tag == 'header' and ident == 'title-block-header' and self.header is None:
            self.header, self._header_at = end, len(self.stack)
        if tag not in VOID:
            self.stack.append((tag, ident, set((attrs.get('class') or '').split())))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] == tag:
                if k == self._header_at and self.header_close is None:
                    self.header_close = self._offset()
                if k == self._nav_at:
                    self._nav_at = -1   # past the nav: no more contents links
                del self.stack[k:]
                return


def _locator(ident):
    return f'<span id="{ident}"></span><a class="mdb-locator" href="#{ident}" style="display:none"></a>'


def prepare(path, rights):
    """Rewrite the print HTML once: a generated anchor first in each contents entry's target, the entry's link pointed
    at it, the locators, and the rights line before the title block's closing tag. Returns the contents entries, in
    order, as {'depth', 'anchor', 'target'}, and whether pandoc wrote a contents nav."""
    text = path.read_text(encoding='utf-8')
    scan = _Scan(text)
    scan.feed(text)
    scan.close()
    reserved = sorted(i for i in scan.ids if i.startswith('mdb-'))
    if reserved:
        raise BuildError(f'the book uses the id {reserved[0]!r}; ids starting with mdb- are reserved for the PDF\'s navigation')
    if scan.header is None or scan.header_close is None:
        raise BuildError('the print document has no title block (header#title-block-header)')
    edits, entries = [], []
    for n, link in enumerate(scan.links, 1):
        target = urllib.parse.unquote(link['href'][1:])
        found = scan.ids.get(target)
        if not found or found['tag'] in VOID or found['region']:
            raise BuildError(f'contents entry {n} points to {target!r}, which is not a printable element of the book')
        anchor = f'mdb-dest-{n}'
        raw = re.sub(r'''href\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)''', f'href="#{anchor}"', link['raw'], count=1)
        edits += [(link['start'], link['end'], raw), (found['end'], found['end'], f'<span id="{anchor}"></span>')]
        entries.append({'depth': link['depth'], 'anchor': anchor, 'target': target})
    edits.append((scan.header, scan.header, _locator(TITLE)))
    if rights:
        edits.append((scan.header_close, scan.header_close, f'<p class="rights">{_locator(RIGHTS)}{html.escape(rights)}</p>'))
    if scan.nav is not None:
        edits.append((scan.nav, scan.nav, _locator(CONTENTS)))
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        text = text[:start] + new + text[end:]
    path.write_text(text, encoding='utf-8')
    return entries, scan.nav is not None


def join_entries(entries, page):
    """The nav's entries with the labels and print visibility the page reported for each anchor, in order."""
    by_id = {e.get('id'): e for e in page}
    if len(page) != len(entries) or set(by_id) != {e['anchor'] for e in entries}:
        raise BuildError('the page reported other contents entries than the print document holds')
    out = []
    for e in entries:
        seen = by_id[e['anchor']]
        if not seen.get('printed'):
            raise BuildError(f"contents entry «{seen.get('label', '')}» points to a heading that isn't printed (display: none or "
                             'visibility: hidden, on it or a parent); show it in print, or keep it out of the contents and bookmarks '
                             'with a lower options.toc_depth')
        out.append(dict(e, label=seen.get('label', '')))
    return out


def named_destinations(reader):
    """Named destinations by name: a NameObject key loses its leading slash once, a text string is taken as it is, and
    either is then URL-decoded, as Chrome percent-encodes the link fragment."""
    from pypdf.generic import NameObject
    out = {}
    for key, dest in reader.named_destinations.items():
        name = str(key)[1:] if isinstance(key, NameObject) else str(key)
        out[urllib.parse.unquote(name)] = dest
    return out


def _fit(dest):
    typ = str(dest['/Type'])
    args = []
    for key in FIT_ARGS.get(typ, ()):
        value = dest.get(key)
        args.append(None if value is None or str(value) == 'null' else round(float(value), 2))
    return typ, tuple(args)


def project(reader, entries, has_nav, rights):
    """The outline to write, as (label, depth, page index, fit type, fit arguments) per entry, from each anchor's
    destination, and the locators' pages. A missing destination is a finalization error."""
    named = named_destinations(reader)
    rows, open_depths = [], []
    for e in entries:
        dest = named.get(e['anchor'])
        if dest is None:
            raise BuildError(f"Chrome wrote no destination for contents entry «{e['label']}»")
        while open_depths and open_depths[-1] >= e['depth']:   # the tree depth, as the written outline nests it
            open_depths.pop()
        open_depths.append(e['depth'])
        rows.append((e['label'], len(open_depths), reader.get_destination_page_number(dest)) + _fit(dest))
    pages = {}
    for ident, wanted in ((TITLE, True), (RIGHTS, bool(rights)), (CONTENTS, has_nav)):
        if wanted:
            if ident not in named:
                raise BuildError(f'Chrome wrote no destination for the {ident} locator')
            pages[ident] = reader.get_destination_page_number(named[ident])
    return rows, pages


def xmp(meta):
    """The XMP packet from the resolved metadata: dc:title and dc:description as language alternatives, dc:creator in
    order, dc:subject, dc:language, dc:rights and dc:publisher when set. XML-escaped on its own."""
    lang = escape(meta['lang'], {'"': '&quot;'})

    def alt(value):
        value = escape(value)
        return (f'<rdf:Alt><rdf:li xml:lang="x-default">{value}</rdf:li><rdf:li xml:lang="{lang}">{value}</rdf:li></rdf:Alt>')

    def items(kind, values):
        return f'<rdf:{kind}>' + ''.join(f'<rdf:li>{escape(v)}</rdf:li>' for v in values) + f'</rdf:{kind}>'
    parts = [f'<dc:title>{alt(meta["title"])}</dc:title>']
    if meta.get('authors'):
        parts.append(f'<dc:creator>{items("Seq", meta["authors"])}</dc:creator>')
    if meta.get('description'):
        parts.append(f'<dc:description>{alt(meta["description"])}</dc:description>')
    if meta.get('subjects'):
        parts.append(f'<dc:subject>{items("Bag", meta["subjects"])}</dc:subject>')
    parts.append(f'<dc:language>{items("Bag", [meta["lang"]])}</dc:language>')
    if meta.get('rights'):
        parts.append(f'<dc:rights>{alt(meta["rights"])}</dc:rights>')
    if meta.get('publisher'):
        parts.append(f'<dc:publisher>{items("Bag", [meta["publisher"]])}</dc:publisher>')
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">' + ''.join(parts) + '</rdf:Description></rdf:RDF></x:xmpmeta>\n'
            '<?xpacket end="w"?>')


def info(meta):
    """The Info entries from the resolved metadata; empty optional fields are left out, never invented."""
    out = {'/Title': meta['title']}
    if meta.get('authors'):
        out['/Author'] = '; '.join(meta['authors'])
    if meta.get('description'):
        out['/Subject'] = meta['description']
    if meta.get('subjects'):
        out['/Keywords'] = ', '.join(meta['subjects'])
    return out


def finalize(source, target, rows, meta):
    """Clone Chrome's PDF whole, replace its outline with the projected one, write Info and XMP, and save to target."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, Fit, NameObject
    reader = PdfReader(source, strict=True)
    writer = PdfWriter(clone_from=reader)
    root = writer._root_object
    if '/Outlines' in root:
        del root['/Outlines']
    parents = []
    for label, depth, page, typ, args in rows:
        while parents and parents[-1][0] >= depth:
            parents.pop()
        item = writer.add_outline_item(label, page, parent=parents[-1][1] if parents else None, fit=Fit(typ, args))
        parents.append((depth, item))
    writer.add_metadata(info(meta))
    stream = DecodedStreamObject()
    stream.set_data(xmp(meta).encode('utf-8'))
    stream[NameObject('/Type')] = NameObject('/Metadata')
    stream[NameObject('/Subtype')] = NameObject('/XML')
    root[NameObject('/Metadata')] = writer._add_object(stream)
    with open(target, 'wb') as out:
        writer.write(out)


def normalized(text):
    """Letters and digits only, NFKC, case-folded: how labels, titles and rights are found in a page's text."""
    return ''.join(c for c in unicodedata.normalize('NFKC', text or '').casefold() if c.isalnum())


def _flat(reader, items, depth=1):
    out, last = [], None
    for item in items:
        if isinstance(item, list):
            out += _flat(reader, item, depth + 1)
            continue
        out.append((str(item.title), depth, reader.get_destination_page_number(item)) + _fit(item))
    return out


def gates(path, rows, meta, toc_title, has_nav, rights):
    """The outline, front-matter and metadata gates on the final file, each {'result', 'problems', ...}."""
    from pypdf import PdfReader
    import xml.etree.ElementTree as ElementTree
    reader = PdfReader(path, strict=True)
    texts = [normalized(p.extract_text() or '') for p in reader.pages]
    problems, skipped = [], []
    found = _flat(reader, reader.outline)
    if found != rows:
        problems.append(f'the outline is not the projected one ({len(found)} entries against {len(rows)})')
    for label, _, page, *_ in rows:
        want = normalized(label)
        if not want:
            skipped.append(f'«{label}»: no letters or digits to find')
            continue
        here = texts[page] + (texts[page + 1] if page + 1 < len(texts) else '')
        at = here.find(want)
        if not 0 <= at < len(texts[page]):
            problems.append(f'«{label}» does not begin on its destination page {page + 1}')
    outline = {'result': 'fail' if problems else 'pass', 'entries': len(rows), 'problems': problems, 'not_checked': skipped}
    named = named_destinations(reader)
    front, not_applicable = [], []
    title_page = reader.get_destination_page_number(named[TITLE]) if TITLE in named else None
    if title_page is None or normalized(meta['title']) not in texts[title_page]:
        front.append('the title is not on the title page')
    if rights:
        page = reader.get_destination_page_number(named[RIGHTS]) if RIGHTS in named else None
        if page != title_page or normalized(rights) not in texts[title_page or 0]:
            front.append('the rights line is not on the title page')
    else:
        not_applicable.append('rights: none configured')
    if has_nav and toc_title:
        page = reader.get_destination_page_number(named[CONTENTS]) if CONTENTS in named else None
        if page is None or normalized(toc_title) not in texts[page]:
            front.append('the contents heading is not on the first contents page')
    else:
        not_applicable.append('contents heading: ' + ('no title for this language' if has_nav else 'contents disabled or pandoc wrote no contents'))
    front_matter = {'result': 'fail' if front else 'pass', 'problems': front, 'not_applicable': not_applicable}
    wrong = []
    for key, value in info(meta).items():
        if (reader.metadata or {}).get(key) != value:
            wrong.append(f'Info {key}')
    for key in ('/Author', '/Subject', '/Keywords'):
        if key not in info(meta) and key in (reader.metadata or {}):
            wrong.append(f'Info {key} set though the configuration has none')
    stream = reader.trailer['/Root'].get('/Metadata')
    if stream is None:
        wrong.append('no XMP metadata stream')
    else:
        stream = stream.get_object()
        if (stream.get('/Type'), stream.get('/Subtype'), stream.get('/Filter')) != ('/Metadata', '/XML', None):
            wrong.append('the XMP stream is not an uncompressed /Metadata /XML stream')
        if stream.get_data().decode('utf-8') != xmp(meta):
            wrong.append('the XMP packet differs from the configuration')
        try:
            ElementTree.fromstring(re.sub(r'<\?xpacket[^>]*\?>', '', stream.get_data().decode('utf-8')))
        except ElementTree.ParseError as e:
            wrong.append(f'the XMP packet is not well-formed XML ({e})')
    lang = str(reader.trailer['/Root'].get('/Lang') or '')
    if lang.lower() != meta['lang'].lower():
        wrong.append(f'catalog /Lang {lang!r}, not {meta["lang"]!r}')
    metadata = {'result': 'fail' if wrong else 'pass', 'problems': wrong}
    return outline, front_matter, metadata
