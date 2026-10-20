"""PDF navigation and metadata (pdfnav): the anchor pass on pandoc-shaped HTML, then the outline, Info, XMP and gates on
small PDFs built here with pypdf. No browser."""
import re

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from mdbindery import pdfnav
from mdbindery.build import BuildError

HTML = '''<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8" /><title>Книга</title></head>
<body>
<section class="pdf-cover"><img src="cover.png" alt="Книга" id="cover-img"></section>
<header id="title-block-header">
<h1 class="title">Книга</h1>
<p class="author">Автор</p>
</header>
<nav id="TOC" role="doc-toc">
<h2 id="toc-title">Содержание</h2>
<ul>
<li><a href="#%D0%B3%D0%BB%D0%B0%D0%B2%D0%B0" id="toc-глава">Глава <em>первая</em></a>
<ul>
<li><a href="#with.dot" id="toc-with.dot">Точка</a></li>
</ul></li>
<li><a href="#second" id="toc-second">Вторая</a></li>
</ul>
</nav>
<h1 id="глава">Глава <em>первая</em></h1>
<p>Текст.</p>
<h2 id="with.dot">Точка</h2>
<h1 id="second" class="unnumbered">Вторая</h1>
</body></html>
'''


def prepared(tmp_path, text=HTML, rights='© 2026 «Автор» & <co>'):
    path = tmp_path / 'book.html'
    path.write_text(text, encoding='utf-8')
    entries, has_nav = pdfnav.prepare(path, rights)
    return path.read_text(encoding='utf-8'), entries, has_nav


def test_prepare_anchors_each_entry_first_in_its_target_and_changes_nothing_else(tmp_path):
    out, entries, has_nav = prepared(tmp_path)
    assert has_nav
    assert [(e['depth'], e['anchor'], e['target']) for e in entries] == [
        (1, 'mdb-dest-1', 'глава'), (2, 'mdb-dest-2', 'with.dot'), (1, 'mdb-dest-3', 'second')]
    assert '<h1 id="глава"><span id="mdb-dest-1"></span>Глава <em>первая</em></h1>' in out
    assert '<h2 id="with.dot"><span id="mdb-dest-2"></span>Точка</h2>' in out
    assert '<a href="#mdb-dest-3" id="toc-second">Вторая</a>' in out
    assert '<header id="title-block-header"><span id="mdb-title-page"></span><a class="mdb-locator" href="#mdb-title-page" style="display:none"></a>' in out
    assert '<nav id="TOC" role="doc-toc"><span id="mdb-contents-page"></span>' in out
    assert ('<p class="rights"><span id="mdb-rights"></span><a class="mdb-locator" href="#mdb-rights" style="display:none"></a>'
            '© 2026 «Автор» &amp; &lt;co&gt;</p></header>') in out
    undone = re.sub(r'<span id="mdb-[^"]*"></span>|<a class="mdb-locator"[^>]*></a>|<p class="rights">.*?</p>(?=</header>)', '', out)
    undone = re.sub(r'href="#mdb-dest-\d+"', 'HREF', undone)
    assert undone == re.sub(r'href="#[^"]*"(?= id="toc-)', 'HREF', HTML)   # only the inserts and the contents hrefs differ


def test_prepare_without_contents_or_rights_adds_only_the_title_locator(tmp_path):
    text = re.sub(r'<nav id="TOC".*?</nav>\n', '', HTML, flags=re.S)
    out, entries, has_nav = prepared(tmp_path, text, rights='')
    assert (entries, has_nav) == ([], False)
    assert 'mdb-contents-page' not in out and 'class="rights"' not in out and 'mdb-title-page' in out


@pytest.mark.parametrize('change, message', [
    (lambda t: t.replace('href="#second"', 'href="#missing"'), "points to 'missing'"),
    (lambda t: t.replace('href="#second"', 'href="#toc-title"'), "points to 'toc-title'"),   # inside the contents
    (lambda t: t.replace('href="#second"', 'href="#cover-img"'), "points to 'cover-img'"),   # a void element in the cover
    (lambda t: t.replace('<p>Текст.</p>', '<p id="mdb-dest-9">Текст.</p>'), 'reserved'),
    (lambda t: t.replace('<header id="title-block-header">', '<header>'), 'no title block'),
])
def test_prepare_refuses_what_it_cannot_anchor(tmp_path, change, message):
    with pytest.raises(BuildError, match=message):
        prepared(tmp_path, change(HTML))


def test_join_entries_takes_the_pages_labels_and_stops_on_a_hidden_heading():
    entries = [{'depth': 1, 'anchor': 'mdb-dest-1', 'target': 'a'}, {'depth': 2, 'anchor': 'mdb-dest-2', 'target': 'b'}]
    page = [{'id': 'mdb-dest-1', 'label': 'Energy E=mc2', 'printed': True}, {'id': 'mdb-dest-2', 'label': 'B', 'printed': True}]
    assert [e['label'] for e in pdfnav.join_entries(entries, page)] == ['Energy E=mc2', 'B']
    with pytest.raises(BuildError, match='other contents entries'):
        pdfnav.join_entries(entries, page[:1])
    with pytest.raises(BuildError, match="«B» points to a heading that isn't printed"):
        pdfnav.join_entries(entries, [page[0], dict(page[1], printed=False)])


def make_pdf(path, pages, dests=()):
    """A PDF of text pages (one line each, Helvetica) with named destinations {name: (page, top)} in /Names/Dests."""
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    for lines in pages:
        page = writer.add_blank_page(612, 792)
        body = ''.join(f'BT /F1 12 Tf 72 {720 - 20 * k} Td ({line}) Tj ET\n' for k, line in enumerate(lines))
        content = DecodedStreamObject()
        content.set_data(body.encode('latin-1'))
        page[NameObject('/Contents')] = writer._add_object(content)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
    for name, (index, top) in dests:
        array = ArrayObject([writer.pages[index].indirect_reference, NameObject('/XYZ'), NumberObject(0), NumberObject(top), NumberObject(0)])
        names = writer._root_object.setdefault(NameObject('/Dests'), DictionaryObject())
        names[NameObject('/' + name)] = array
    writer._root_object[NameObject('/Lang')] = TextStringObject('en')
    with open(path, 'wb') as out:
        writer.write(out)


META = {'title': 'A & B <c> "d" \'e\'', 'authors': ['Ann & Bo', 'Ёж <Колючий>'], 'description': 'Desc & "more"',
        'subjects': ['One', 'Two & three'], 'rights': '© 2026 "R" & <S>', 'publisher': 'P & Q', 'lang': 'en'}


def built(tmp_path, pages=None, entries=None, meta=META, has_nav=True):
    pages = pages or [['cover'], ['A & B <c> "d" \'e\'', '2026 "R" & <S>'], ['Contents'], ['Chapter One'], ['Part', 'Chapter One']]
    dests = [('mdb-title-page', (1, 700)), ('mdb-rights', (1, 680)), ('mdb-contents-page', (2, 700)),
             ('mdb-dest-1', (3, 720)), ('mdb-dest-2', (4, 700)), ('mdb-dest-3', (4, 680))]
    source, final = tmp_path / 'chrome.pdf', tmp_path / 'final.pdf'
    make_pdf(source, pages, dests)
    entries = entries if entries is not None else [
        {'depth': 1, 'anchor': 'mdb-dest-1', 'label': 'Chapter One'}, {'depth': 2, 'anchor': 'mdb-dest-2', 'label': 'Part'},
        {'depth': 4, 'anchor': 'mdb-dest-3', 'label': 'Chapter One'}]   # a repeated title, two entries on one page, a skipped level
    rows, pages_of = pdfnav.project(PdfReader(source), entries, has_nav, meta['rights'])
    pdfnav.finalize(source, final, rows, meta)
    return final, rows, pages_of


def test_outline_metadata_and_front_matter_pass_on_a_faithful_file(tmp_path):
    final, rows, pages_of = built(tmp_path)
    assert pages_of == {'mdb-title-page': 1, 'mdb-rights': 1, 'mdb-contents-page': 2}
    assert [r[:3] for r in rows] == [('Chapter One', 1, 3), ('Part', 2, 4), ('Chapter One', 3, 4)]   # level 4 nests at tree depth 3
    outline, front, metadata = pdfnav.gates(final, rows, META, 'Contents', True, META['rights'])
    assert (outline['result'], front['result'], metadata['result']) == ('pass', 'pass', 'pass'), (outline, front, metadata)
    reader = PdfReader(final)
    assert reader.metadata['/Author'] == 'Ann & Bo; Ёж <Колючий>' and reader.metadata['/Keywords'] == 'One, Two & three'
    xmp = reader.trailer['/Root']['/Metadata'].get_object().get_data().decode('utf-8')
    assert '<rdf:li>Ёж &lt;Колючий&gt;</rdf:li>' in xmp and 'xml:lang="en">Desc &amp; "more"<' in xmp
    nested = reader.outline
    assert isinstance(nested[1], list) and isinstance(nested[1][1], list)   # Part under Chapter One, the repeated title under Part


def test_empty_optional_metadata_is_left_out(tmp_path):
    meta = dict(META, authors=[], description='', subjects=[], rights='', publisher='')
    final, rows, _ = built(tmp_path, meta=meta)
    reader = PdfReader(final)
    assert not {'/Author', '/Subject', '/Keywords'} & set(reader.metadata)
    xmp = reader.trailer['/Root']['/Metadata'].get_object().get_data().decode('utf-8')
    assert 'dc:creator' not in xmp and 'dc:rights' not in xmp and 'dc:publisher' not in xmp
    assert pdfnav.gates(final, rows, meta, 'Contents', True, '')[2]['result'] == 'pass'


@pytest.mark.parametrize('alter', [
    lambda rows: [('Chapter 1',) + rows[0][1:]] + rows[1:],   # a wrong label
    lambda rows: rows[:2] + [rows[2][:1] + (1,) + rows[2][2:]],   # a wrong depth
    lambda rows: [rows[0][:2] + (2,) + rows[0][3:]] + rows[1:],   # a wrong but valid page
    lambda rows: [rows[0][:4] + ((0.0, 500.0, 0.0),)] + rows[1:],   # an altered fit
])
def test_the_outline_gate_fails_on_any_difference_from_the_projection(tmp_path, alter):
    final, rows, _ = built(tmp_path)
    written = tmp_path / 'altered.pdf'
    pdfnav.finalize(tmp_path / 'chrome.pdf', written, alter(rows), META)
    assert pdfnav.gates(written, rows, META, 'Contents', True, META['rights'])[0]['result'] == 'fail'


def test_a_label_not_on_its_destination_page_fails(tmp_path):
    entries = [{'depth': 1, 'anchor': 'mdb-dest-1', 'label': 'Elsewhere'}]
    final, rows, _ = built(tmp_path, entries=entries)
    outline = pdfnav.gates(final, rows, META, 'Contents', True, META['rights'])[0]
    assert outline['result'] == 'fail' and 'does not begin on its destination page' in outline['problems'][0]


def test_the_front_matter_gate_finds_each_part_by_its_locator(tmp_path):
    pages = [['cover'], ['A & B <c> "d" \'e\''], ['Contents'], ['Chapter One'], ['Part', 'Chapter One']]   # no rights text
    final, rows, _ = built(tmp_path, pages=pages)
    front = pdfnav.gates(final, rows, META, 'Contents', True, META['rights'])[1]
    assert front['problems'] == ['the rights line is not on the title page']
    front = pdfnav.gates(final, rows, META, 'Inhalt', True, META['rights'])[1]
    assert 'the contents heading is not on the first contents page' in front['problems']
    front = pdfnav.gates(final, rows, META, '', True, '')[1]
    assert front['result'] == 'pass' and len(front['not_applicable']) == 2


def test_no_contents_writes_no_outline(tmp_path):
    final, rows, _ = built(tmp_path, entries=[], has_nav=False)
    assert rows == [] and PdfReader(final).outline == []
    outline, front, _ = pdfnav.gates(final, rows, META, '', False, META['rights'])
    assert outline['result'] == 'pass' and front['result'] == 'pass'


def test_a_missing_destination_is_a_finalization_error(tmp_path):
    source = tmp_path / 'chrome.pdf'
    make_pdf(source, [['a'], ['b']], [('mdb-title-page', (1, 700))])
    with pytest.raises(BuildError, match='no destination for contents entry «X»'):
        pdfnav.project(PdfReader(source), [{'depth': 1, 'anchor': 'mdb-dest-1', 'label': 'X'}], False, '')
    with pytest.raises(BuildError, match='mdb-rights locator'):
        pdfnav.project(PdfReader(source), [], False, 'rights')


def test_destination_names_are_normalized_by_their_pdf_type():
    class Reader:
        named_destinations = {NameObject('/mdb-dest-1'): 'a', TextStringObject('%D0%B3%D0%BB'): 'b', NameObject('/with.dot'): 'c'}
    assert pdfnav.named_destinations(Reader()) == {'mdb-dest-1': 'a', 'гл': 'b', 'with.dot': 'c'}


def test_a_wrong_metadata_field_fails_the_metadata_gate(tmp_path):
    final, rows, _ = built(tmp_path)
    assert pdfnav.gates(final, rows, dict(META, title='Another'), 'Contents', True, META['rights'])[2]['result'] == 'fail'
