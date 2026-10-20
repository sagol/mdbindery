"""PDF integration and EPUB compatibility checks. Never build source fixtures in place."""
import json
import os
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from conftest import EXAMPLES, FIXTURES, needs_pandoc, validate_if_installed
from mdbindery import config, tools
from mdbindery.build import BuildError, build
from mdbindery.cli import main, parser
from mdbindery.pdf import validate_pdf


def quiet(*args):
    pass


@pytest.fixture
def browser(monkeypatch):
    try:
        tools.puppeteer_runtime()
    except RuntimeError:
        if os.environ.get('MDBINDERY_REQUIRE_TOOLS'):
            pytest.fail('PDF tests require installed Node/Puppeteer')
        pytest.skip('Node/Puppeteer not installed')
    # The CI runner already disables Chromium sandbox for Mermaid/Ace. PDF requires explicit opt-out.
    if os.environ.get('CI') or (hasattr(os, 'geteuid') and os.geteuid() == 0):
        monkeypatch.setenv('MDBINDERY_NO_SANDBOX', '1')


def small_book(tmp_path, body='Visible paragraph with words that must survive export.'):
    book = tmp_path / 'book'
    book.mkdir()
    (book / '01-chapter.md').write_text('# First chapter\n\n' + body + '\n', encoding='utf-8')
    (book / 'mdbindery.yaml').write_text('metadata:\n  title: A PDF book\n  identifier: urn:uuid:test\n'
                                       '  date: 2026-09-26\noptions:\n  mermaid: placeholder\n', encoding='utf-8')
    return config.load(book)


@pytest.mark.parametrize('settings', [None, False, [], {'page_size': 'A3'}, {'margin_mm': 0},
                                      {'margin_mm': 100}, {'margin_mm': float('nan')},
                                      {'page_numbers': 'perhaps'}])
def test_pdf_settings_reject_invalid_values(settings):
    data = config.deep_merge(config.DEFAULTS, {'options': {'pdf': settings}})
    with pytest.raises(config.ConfigError, match='options.pdf'):
        config.validate(data)


def test_pdf_cli_is_opt_in():
    assert parser().parse_args(['build']).format == 'epub'
    assert parser().parse_args(['build', '--format', 'pdf']).format == 'pdf'
    with pytest.raises(SystemExit):
        parser().parse_args(['build', '--format', 'docx'])


@needs_pandoc
def test_missing_browser_keeps_old_pdf_and_epub_reports(tmp_path, monkeypatch):
    cfg = small_book(tmp_path)
    out = tmp_path / 'out'
    (out / 'reports').mkdir(parents=True)
    old = out / f'{cfg["slug"]}.pdf'
    old.write_bytes(b'previous PDF')
    (out / 'reports' / 'build.json').write_text('EPUB report')
    monkeypatch.setattr(tools, 'puppeteer_runtime', lambda: (_ for _ in ()).throw(RuntimeError('missing browser')))
    with pytest.raises(BuildError, match='missing browser'):
        build(cfg, out_dir=out, output_format='pdf', log=quiet)
    report = json.loads((out / 'reports' / 'pdf' / 'build.json').read_text())
    assert report['failed_stage'] == 'pdf_dependencies'
    assert 'earlier build' in report['artifact']
    assert old.read_bytes() == b'previous PDF'
    assert (out / 'reports' / 'build.json').read_text() == 'EPUB report'


def test_pdf_rejects_empty_document(tmp_path):
    path = tmp_path / 'empty.pdf'
    PdfWriter().write(path)
    with pytest.raises(BuildError, match='PDF cannot be read'):
        validate_pdf(path, 'Expected content', config.DEFAULTS['options'])


@needs_pandoc
def test_epub_never_calls_pdf_renderer(copy_book, tmp_path, monkeypatch):
    from mdbindery import pdf
    def forbidden(*args, **kwargs):
        pytest.fail('EPUB must not use PDF dependencies or renderer')
    monkeypatch.setattr(pdf, 'requirements', forbidden)
    monkeypatch.setattr(pdf, 'render', forbidden)
    cfg = validate_if_installed(config.load(copy_book(FIXTURES / 'ru-book')))
    cfg.opts['mermaid'] = 'placeholder'
    summary = build(cfg, out_dir=tmp_path / 'out', run_ace=False, log=quiet)
    assert summary['ok'] and 'pdf' not in summary
    assert set(summary['gates']) >= {'wordcount', 'epubcheck', 'ace'}


@needs_pandoc
def test_pdf_sample_and_epub_can_share_output(copy_book, tmp_path, browser):
    from mdbindery.preview import preview
    cfg = validate_if_installed(config.load(copy_book(EXAMPLES / 'sample-book')))
    out = tmp_path / 'out'
    epub = build(cfg, out_dir=out, run_ace=False, log=quiet)
    epub_bytes = Path(epub['epub']).read_bytes()
    report_bytes = (out / 'reports' / 'build.json').read_bytes()
    summary = build(cfg, out_dir=out, output_format='pdf', log=quiet)
    assert summary['ok'], summary
    assert 'epubcheck' not in summary['gates'] and 'ace' not in summary['gates']
    assert Path(epub['epub']).read_bytes() == epub_bytes
    assert (out / 'reports' / 'build.json').read_bytes() == report_bytes
    assert (out / 'reports' / 'pdf' / 'build.json').is_file()
    reader = PdfReader(summary['pdf'])
    text = '\n'.join(p.extract_text() or '' for p in reader.pages)
    assert len(reader.pages) >= 5
    assert 'Writing a Book in Markdown' in text and 'Sample Author' in text
    assert 'Contributing' not in text
    assert 'Why Markdown' in text and 'Checklist' in text
    assert any('/XObject' in p['/Resources'] for p in reader.pages)
    annotations = [a.get_object() for p in reader.pages for a in p.get('/Annots', [])]
    assert any(a.get('/Dest') or a.get('/A', {}).get('/S') == '/GoTo' for a in annotations)
    assert any(a.get('/A', {}).get('/S') == '/URI' for a in annotations)
    # Reused browser discovery must still produce actual EPUB screenshots.
    shots = preview(epub['epub'], tmp_path / 'shots', pages=['text/ch001.xhtml'])
    assert shots and Path(shots[0]).read_bytes().startswith(b'\x89PNG')
    strict = dict(cfg.opts, wordcount_tolerance=0, wordcount_min_words=0)
    check = validate_pdf(summary['pdf'], text + ' MISSING_SENTINEL_XYZ', strict)
    assert check['result'] == 'fail' and check['missing_words'] >= 1


@needs_pandoc
def test_pdf_russian_unicode_paths_letter_and_no_page_numbers(copy_book, tmp_path, browser):
    book = copy_book(FIXTURES / 'ru-book')
    moved = tmp_path / 'Книга с пробелами'
    book.rename(moved)
    cfg = config.load(moved)
    cfg.opts['pdf'].update(page_size='Letter', margin_mm=15, page_numbers=False)
    summary = build(cfg, out_dir=tmp_path / 'Выход с пробелами', output_format='pdf', log=quiet)
    assert summary['ok'], summary
    reader = PdfReader(summary['pdf'])
    text = '\n'.join(p.extract_text() or '' for p in reader.pages)
    assert 'Пробная книга' in text and '[Milchin]' in text and 'кавычками' in text
    assert float(reader.pages[0].mediabox.width) == pytest.approx(612, abs=1)
    assert float(reader.pages[0].mediabox.height) == pytest.approx(792, abs=1)


@needs_pandoc
@pytest.mark.parametrize('resource', ['https://example.invalid/blocked.png', 'file:///etc/passwd'])
def test_pdf_blocks_css_resource_reads_and_preserves_previous_output(tmp_path, browser, resource):
    cfg = small_book(tmp_path)
    css = cfg.base / 'bad.css'
    css.write_text('body { background-image: url("' + resource + '"); }')
    cfg.opts['extra_css'] = str(css)
    out = tmp_path / 'out'
    out.mkdir()
    previous = out / f'{cfg["slug"]}.pdf'
    previous.write_bytes(b'previous PDF')
    with pytest.raises(BuildError, match='PDF resources or internal links failed'):
        build(cfg, out_dir=out, output_format='pdf', log=quiet)
    assert previous.read_bytes() == b'previous PDF'
    assert not list(out.glob('*.part'))
    report = json.loads((out / 'reports' / 'pdf' / 'build.json').read_text())
    assert not report['ok'] and report['failed_stage'] == 'pdf'


@needs_pandoc
def test_pdf_cli_failure_writes_separate_log(tmp_path, monkeypatch):
    cfg = small_book(tmp_path)
    monkeypatch.setattr(tools, 'puppeteer_runtime', lambda: (_ for _ in ()).throw(RuntimeError('missing browser')))
    out = tmp_path / 'out'
    assert main(['build', str(cfg.base), '--format', 'pdf', '--out', str(out), '--quiet']) == 2
    assert 'missing browser' in (out / 'reports' / 'pdf' / 'build.log').read_text()


@needs_pandoc
def test_pdf_literal_metadata_and_script_input(tmp_path, browser):
    cfg = small_book(tmp_path, 'Text with [a link](#first-chapter).\n\n<script>throw new Error("RUN");</script>')
    cfg.meta['title'] = 'Using <div> & C_sharp_ [draft]'
    cfg.opts['toc'] = False
    summary = build(cfg, out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary
    reader = PdfReader(summary['pdf'])
    text = '\n'.join(p.extract_text() or '' for p in reader.pages)
    assert cfg.meta['title'] in text
    assert 'throw new Error' not in text


@needs_pandoc
def test_pdf_hidden_body_fails_text_gate(tmp_path, browser):
    cfg = small_book(tmp_path, ' '.join('word' + str(i) for i in range(100)))
    css = cfg.base / 'hide.css'
    css.write_text('body { visibility: hidden; }')
    cfg.opts['extra_css'] = str(css)
    cfg.opts['toc'] = False   # with contents, the hidden chapter heading stops the build first (the next test)
    summary = build(cfg, out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert not summary['ok'] and summary['gates']['pdf']['result'] == 'fail'
    assert summary['gates']['pdf']['missing_words'] >= 100


@needs_pandoc
def test_pdf_contents_entry_for_a_hidden_heading_stops_the_build(tmp_path, browser):
    cfg = small_book(tmp_path)
    css = cfg.base / 'hide.css'
    css.write_text('h1:not(.title) { visibility: hidden; }')
    cfg.opts['extra_css'] = str(css)
    with pytest.raises(BuildError, match="isn't printed"):
        build(cfg, out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert not list((tmp_path / 'out').glob('*.pdf*'))   # no PDF, and no temporary file left


@needs_pandoc
def test_pdf_missing_font_is_a_build_error(tmp_path, browser):
    cfg = small_book(tmp_path)
    cfg.opts['embed_fonts'] = ['fonts/missing.ttf']
    with pytest.raises(BuildError, match='PDF asset not found'):
        build(cfg, out_dir=tmp_path / 'out', output_format='pdf', log=quiet)


@needs_pandoc
def test_pdf_does_not_automatically_disable_sandbox(tmp_path, browser, monkeypatch):
    from mdbindery import pdf
    cfg = small_book(tmp_path)
    monkeypatch.delenv('MDBINDERY_NO_SANDBOX', raising=False)
    monkeypatch.setattr(tools, 'no_sandbox', lambda: True)
    original = pdf.run
    launches = []
    def fail_browser(command, **kwargs):
        if str(command[1]).endswith('pdf.js'):
            launches.append(json.loads(Path(command[2]).read_text()))
            raise BuildError('No usable sandbox')
        return original(command, **kwargs)
    monkeypatch.setattr(pdf, 'run', fail_browser)
    with pytest.raises(BuildError, match='No usable sandbox'):
        build(cfg, out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert len(launches) == 1 and launches[0]['args'] == []


@needs_pandoc
def test_pdf_without_identifier_preserves_source_config(tmp_path, browser):
    cfg = small_book(tmp_path)
    path = cfg.base / 'mdbindery.yaml'
    path.write_text(path.read_text().replace('  identifier: urn:uuid:test\n', ''))
    before = path.read_bytes()
    cfg = config.load(cfg.base)
    assert not cfg.meta['identifier']
    summary = build(cfg, out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary
    assert path.read_bytes() == before
    assert not cfg.meta['identifier']


@needs_pandoc
def test_pdf_missing_python_extra_fails_before_analysis(tmp_path, monkeypatch):
    import sys
    from mdbindery import build as pipeline
    cfg = small_book(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('Missing PDF dependencies must fail before chapter preparation')
    monkeypatch.setitem(sys.modules, 'pypdf', None)
    monkeypatch.setattr(pipeline, 'analyze', forbidden)
    out = tmp_path / 'out'
    with pytest.raises(BuildError, match='PDF needs pypdf'):
        build(cfg, out_dir=out, output_format='pdf', log=quiet)
    report = json.loads((out / 'reports' / 'pdf' / 'build.json').read_text())
    assert report['failed_stage'] == 'pdf_dependencies' and report['format'] == 'pdf'


@needs_pandoc
def test_epub_preview_with_symlinked_temp_directory(copy_book, tmp_path, browser, monkeypatch):
    from mdbindery import preview as preview_module
    cfg = validate_if_installed(config.load(copy_book(FIXTURES / 'ru-book')))
    summary = build(cfg, out_dir=tmp_path / 'out', run_ace=False, log=quiet)
    real = tmp_path / 'real-temp'
    real.mkdir()
    alias = tmp_path / 'temp-alias'
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('Creating directory symlinks requires privileges on this system')
    original = preview_module.tempfile.mkdtemp
    def aliased_temp(**kwargs):
        return original(dir=alias, **kwargs)
    monkeypatch.setattr(preview_module.tempfile, 'mkdtemp', aliased_temp)
    shots = preview_module.preview(summary['epub'], tmp_path / 'shots', pages=['text/ch001.xhtml'])
    assert shots and Path(shots[0]).read_bytes().startswith(b'\x89PNG')
    assert not list(real.iterdir())


def nav_book(tmp_path, files, lang='ru', toc=True, toc_depth=2, toc_title='', rights='© 2026 «Автор» & Co', css='', extra=''):
    """A small book for the outline and metadata checks: each file is one chapter on its own page."""
    book = tmp_path / 'book'
    book.mkdir()
    for name, text in files.items():
        (book / name).write_text(text, encoding='utf-8')
    if css:
        (book / 'extra.css').write_text(css, encoding='utf-8')
    (book / 'mdbindery.yaml').write_text(
        'metadata:\n  title: "Навигация & <тест>"\n  authors: [Анна Первая, "Bo & Co"]\n  identifier: urn:uuid:test\n'
        f'  date: 2026-10-20\n  lang: {lang}\n  rights: {json.dumps(rights)}\n  description: "Описание & <ещё>"\n'
        '  subjects: [Марс, "A & B"]\n  publisher: Издатель\n'
        f'options:\n  mermaid: placeholder\n  toc: {str(toc).lower()}\n  toc_depth: {toc_depth}\n  toc_title: {json.dumps(toc_title)}\n'
        + ('  extra_css: extra.css\n' if css else '') + extra, encoding='utf-8')
    return config.load(book)


def outline_of(reader):
    out = []

    def walk(items, depth):
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
            else:
                out.append((str(item.title), depth, reader.get_destination_page_number(item)))
    walk(reader.outline, 1)
    return out


CHAPTERS = {'01.md': '# Первая глава\n\nТекст первой главы.\n\n## Раздел один\n\nЕщё текст.\n',
            '02.md': '# Вторая глава\n\nТекст второй главы.\n\n### Глубже\n\nСовсем глубоко.\n',
            '03.md': '# Третья глава\n\nТекст третьей главы.\n'}


@needs_pandoc
def test_pdf_outline_metadata_and_front_matter(tmp_path, browser):
    summary = build(nav_book(tmp_path, CHAPTERS), out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    gates = summary['gates']
    assert [gates['pdf']['words']] + [gates['pdf'][k]['result'] for k in ('outline', 'front_matter', 'metadata')] == ['pass'] * 4
    reader = PdfReader(summary['pdf'])
    found = outline_of(reader)
    assert [(t, d) for t, d, _ in found] == [('Первая глава', 1), ('Раздел один', 2), ('Вторая глава', 1), ('Третья глава', 1)]
    pages = [p for _, _, p in found]
    assert pages[0] < pages[2] < pages[3] and pages[1] == pages[0]   # chapters on their own pages; the section on its chapter's
    for title, _, page in found:
        assert title in reader.pages[page].extract_text()
    info = reader.metadata
    assert (info['/Title'], info['/Author'], info['/Subject'], info['/Keywords']) == (
        'Навигация & <тест>', 'Анна Первая; Bo & Co', 'Описание & <ещё>', 'Марс, A & B')
    assert reader.trailer['/Root']['/Lang'] == 'ru' and '/StructTreeRoot' in reader.trailer['/Root']
    xmp = reader.trailer['/Root']['/Metadata'].get_object().get_data().decode('utf-8')
    assert '<rdf:Seq><rdf:li>Анна Первая</rdf:li><rdf:li>Bo &amp; Co</rdf:li></rdf:Seq>' in xmp and '<dc:publisher>' in xmp
    title_page = next(i for i, p in enumerate(reader.pages) if 'Навигация' in (p.extract_text() or ''))
    assert '© 2026 «Автор» & Co' in reader.pages[title_page].extract_text()
    assert 'Содержание' in reader.pages[title_page + 1].extract_text()
    links = sum(1 for p in reader.pages for a in (p.get('/Annots') or []) if a.get_object().get('/Subtype') == '/Link')
    assert links == 4   # the four contents links; the hidden locators add none


@needs_pandoc
@pytest.mark.parametrize('lang, toc_title, heading', [('de', 'Inhalt', 'Inhalt'), ('de', '', None), ('en-US', '', 'Contents')])
def test_pdf_contents_heading_follows_toc_title(tmp_path, browser, lang, toc_title, heading):
    summary = build(nav_book(tmp_path, CHAPTERS, lang=lang, toc_title=toc_title), out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    front = summary['gates']['pdf']['front_matter']
    reader = PdfReader(summary['pdf'])
    contents = next(i for i, p in enumerate(reader.pages) if 'Первая глава' in (p.extract_text() or ''))
    if heading:
        assert heading in reader.pages[contents].extract_text() and not front['not_applicable'][1:]
    else:
        assert any('no title for this language' in n for n in front['not_applicable'])


@needs_pandoc
def test_pdf_without_contents_still_checks_the_rights_line(tmp_path, browser):
    summary = build(nav_book(tmp_path, CHAPTERS, toc=False), out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    assert PdfReader(summary['pdf']).outline == []
    front = summary['gates']['pdf']['front_matter']
    assert front['result'] == 'pass' and any('contents disabled' in n for n in front['not_applicable'])


@needs_pandoc
@pytest.mark.parametrize('depth, entries', [(1, 3), (2, 4), (6, 5)])
def test_pdf_outline_follows_toc_depth(tmp_path, browser, depth, entries):
    summary = build(nav_book(tmp_path, CHAPTERS, toc_depth=depth), out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    found = outline_of(PdfReader(summary['pdf']))
    assert len(found) == entries
    if depth == 6:
        assert ('Глубже', 2) in [(t, d) for t, d, _ in found]   # a skipped level nests under its chapter, as the contents does


@needs_pandoc
def test_pdf_repeated_titles_and_two_headings_on_one_page_keep_their_own_destinations(tmp_path, browser):
    files = {'01.md': '# Интерлюдия\n\nПервая.\n', '02.md': '# Интерлюдия\n\nВторая.\n\n## Альфа\n\nТекст.\n\n## Бета\n\nТекст.\n'}
    summary = build(nav_book(tmp_path, files), out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    found = outline_of(PdfReader(summary['pdf']))
    assert [t for t, _, _ in found] == ['Интерлюдия', 'Интерлюдия', 'Альфа', 'Бета']
    assert found[0][2] < found[1][2] == found[2][2] == found[3][2]


@needs_pandoc
def test_pdf_display_contents_heading_gets_its_bookmark(tmp_path, browser):
    summary = build(nav_book(tmp_path, CHAPTERS, css='body > h1:nth-of-type(2) { display: contents; }'), out_dir=tmp_path / 'out',
                    output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    assert 'Вторая глава' in [t for t, _, _ in outline_of(PdfReader(summary['pdf']))]


@needs_pandoc
@pytest.mark.parametrize('css, prose, veil', [
    ('body > h1:nth-of-type(2) { display: none; }', '', False),
    ('body > h1:nth-of-type(2) { visibility: hidden; }', 'Слова Вторая глава повторены здесь в тексте.', False),   # the label is still on the page
    ('.veil { visibility: hidden; }', '', True),   # hidden through a parent (raw HTML, which GFM passes through)
])
def test_pdf_contents_entry_whose_heading_is_not_printed_stops_the_build(tmp_path, browser, css, prose, veil):
    heading = '<div class="veil">\n\n# Вторая глава\n\n</div>\n\n' if veil else '# Вторая глава\n\n'
    files = dict(CHAPTERS, **{'02.md': heading + prose + '\n\nТекст второй главы.\n'})
    with pytest.raises(BuildError, match="«Вторая глава» points to a heading that isn't printed"):
        build(nav_book(tmp_path, files, css=css), out_dir=tmp_path / 'out', output_format='pdf', log=quiet)


@needs_pandoc
def test_pdf_math_headings_carry_their_rendered_labels(tmp_path, browser):
    files = {'01.md': '# Energy $E=mc^2$\n\nText.\n', '02.md': '# Water $\\mathrm{H_2O}$\n\nText.\n'}
    summary = build(nav_book(tmp_path, files, lang='en-US'), out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    labels = [t for t, _, _ in outline_of(PdfReader(summary['pdf']))]
    assert labels == ['Energy E=mc2', 'Water H2O'] and summary['gates']['pdf']['outline']['result'] == 'pass'


@needs_pandoc
def test_pdf_bookmarks_name_images_by_their_alt_text(tmp_path, browser):
    book = nav_book(tmp_path, {'01.md': '# ![Logo](logo.png)\n\nText.\n', '02.md': '# Map ![of Mars](logo.png) today\n\nText.\n'}, lang='en-US')
    Image.new('RGB', (20, 20), 'white').save(book.base / 'logo.png')
    summary = build(book, out_dir=tmp_path / 'out', output_format='pdf', log=quiet)
    assert summary['ok'], summary['gates']
    labels = [t for t, _, _ in outline_of(PdfReader(summary['pdf']))]
    assert labels == ['Logo', 'Map of Mars today']
    assert any('Logo' in n for n in summary['gates']['pdf']['outline']['not_checked'])   # an image-only heading has no page text
