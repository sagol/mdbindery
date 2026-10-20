"""PDF output from the prepared Pandoc document, using the existing Chromium tools."""
import hashlib
import html
import json
import os
import re
import shutil
import unicodedata
from collections import Counter
from pathlib import Path

from . import config, pdfnav, tools
from .build import BuildError, DATA, meta_value, run


JS = r"""
const fs = require('fs');
const url = require('url');
const puppeteer = require('puppeteer');
const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  const browser = await puppeteer.launch({headless: 'shell', args: jobs.args});
  try {
    const page = await browser.newPage();
    const allowed = new Set(jobs.files.map(p => fs.realpathSync(p)));
    const failures = new Set();
    await page.setJavaScriptEnabled(false);
    await page.setRequestInterception(true);
    page.on('request', request => {
      let ok = false;
      try {
        const u = new URL(request.url());
        ok = u.protocol === 'data:' || (u.protocol === 'file:' &&
          allowed.has(fs.realpathSync(url.fileURLToPath(u))));
      } catch (_) {}
      if (ok) request.continue();
      else { failures.add('blocked resource'); request.abort(); }
    });
    page.on('requestfailed', () => failures.add('failed resource'));
    await page.emulateMediaType('print');
    await page.goto(url.pathToFileURL(jobs.html).href, {waitUntil: 'load', timeout: 60000});
    await page.evaluate(() => document.fonts.ready);
    const documentState = await page.evaluate(() => {
      const source = document.body.cloneNode(true);
      source.querySelectorAll('annotation, script, style').forEach(n => n.remove());
      return {
      text: source.textContent,
      entries: Array.from(document.querySelectorAll('nav#TOC a[data-mdb-dest]')).map(a => {
        const id = a.getAttribute('data-mdb-dest');
        const anchor = document.getElementById(id);
        const text = a.cloneNode(true);   // the heading's words for its page check, by the word gate's extraction rule:
        text.querySelectorAll('annotation, script, style').forEach(n => n.remove());   // no TeX annotation, script or style
        text.querySelectorAll('br').forEach(n => n.replaceWith(' '));
        let label = '', words = '', shows = false;   // the bookmark is what the printed entry shows: text that lays out and is
        const unresolved = [];   // inked (a display: contents link has no box of its own), and an image by its alt text; words
        const add = (piece, alt) => {   // leaves the alt text out, for the gate that finds each entry in the contents' own text
          label += piece;
          if (!alt) words += piece;
        };
        const alpha = color => {   // a computed colour's alpha, from its own arguments: after a slash (rgb(… / a), color(space … / a),
          if (color === 'transparent') return 0;   // oklch(… / a)), or the fourth of rgba(r, g, b, a); none of those is opaque
          const args = /\(([^()]*)\)\s*$/.exec(color);
          const value = !args ? null : args[1].includes('/') ? args[1].split('/').pop()
            : args[1].split(',').length === 4 ? args[1].split(',')[3] : null;
          if (value === null) return 1;
          const a = value.trim();
          return a === 'none' ? 0 : parseFloat(a) / (a.endsWith('%') ? 100 : 1);
        };
        const inks = (el, style) => {   // its text is drawn: a visible fill colour, and no zero opacity on it or above it
          if (alpha(style.webkitTextFillColor) === 0) return false;
          for (let e = el; e; e = e.parentElement) if (parseFloat(getComputedStyle(e).opacity) === 0) return false;
          return parseFloat(style.opacity) !== 0;
        };
        const generated = (el, which) => {   // the words a visible ::before or ::after prints: its strings and attr() values;
          const style = getComputedStyle(el, which);   // a counter or a quote mark is not readable here, so it is reported
          const value = style.content;
          if (!value || value === 'none' || value === 'normal' || style.display === 'none' || style.visibility !== 'visible'
              || !inks(el, style)) return;
          let i = 0, printed = '';
          while (i < value.length) {
            const c = value[i];
            if (c === '"' || c === "'") {
              let j = i + 1;
              while (j < value.length && value[j] !== c) {
                const hex = value[j] === '\\' && /^[0-9a-fA-F]{1,6} ?/.exec(value.slice(j + 1));
                if (hex) { printed += String.fromCodePoint(parseInt(hex[0], 16)); j += 1 + hex[0].length; }
                else if (value[j] === '\\') { printed += value[j + 1] || ''; j += 2; }
                else printed += value[j++];
              }
              i = j + 1;
            } else if (c === '/') {
              break;   // the alternative text after it is for speech, not print
            } else if (/\s/.test(c)) {
              i++;
            } else {
              const part = /^[-\w]+(\((?:[^()"']|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\([^()]*\))*\))?/.exec(value.slice(i));
              if (!part) { unresolved.push(value); return; }
              const name = part[0].split('(')[0].toLowerCase(), args = part[1] ? part[1].slice(1, -1).trim() : '';
              if (name === 'attr' && /^[-\w]+$/.test(args)) printed += el.getAttribute(args) || '';
              else if (['attr', 'counter', 'counters', 'open-quote', 'close-quote'].includes(name)) unresolved.push(part[0]);
              // no-open-quote and no-close-quote print nothing; url(), gradients and image sets are images, no words
              i += part[0].length;
            }
          }
          if (printed.trim()) shows = true;
          add(printed);
        };
        const hides = el => {   // nothing inside it prints, its ::before and ::after included, whatever their own style says
          const style = getComputedStyle(el);
          return style.display === 'none' || style.contentVisibility === 'hidden';
        };
        const walk = node => {
          if (node.nodeType === Node.ELEMENT_NODE) generated(node, '::before');
          for (const n of node.childNodes) {
            if (n.nodeType === Node.TEXT_NODE) {
              if (!n.data.trim()) { add(' '); continue; }   // a space that wraps a line may have no box of its own
              const range = document.createRange(), style = getComputedStyle(n.parentElement);
              range.selectNodeContents(n);
              if (range.getClientRects().length > 0 && style.visibility === 'visible' && inks(n.parentElement, style)) {
                add(n.data);
                shows = true;
              }
            } else if (n.nodeType === Node.ELEMENT_NODE && !['annotation', 'script', 'style'].includes(n.localName) && !hides(n)) {
              if (n.localName === 'br') add(' ');
              else if (n.localName !== 'img') walk(n);
              else if (n.checkVisibility({visibilityProperty: true, opacityProperty: true})) {
                add(' ' + (n.getAttribute('alt') || '') + ' ', true);
                shows = true;
              }
            }
          }
          if (node.nodeType === Node.ELEMENT_NODE) generated(node, '::after');
        };
        let lays = true;   // the link and every ancestor (its list item, the nav) lay out
        for (let el = a; el; el = el.parentElement) if (hides(el)) lays = false;
        if (lays) walk(a);
        return {id, label: label.replace(/\s+/g, ' ').trim(), words: words.replace(/\s+/g, ' ').trim(),
          text: text.textContent.replace(/\s+/g, ' ').trim(), listed: shows, unresolved,
          printed: !!anchor && anchor.checkVisibility({visibilityProperty: true, opacityProperty: true})};
      }),
      brokenImages: Array.from(document.images).filter(i => !i.complete || !i.naturalWidth).length,
      brokenFonts: Array.from(document.fonts).filter(f => f.status === 'error').length,
      brokenLinks: Array.from(document.querySelectorAll('a[href^="#"]')).filter(a => {
        try { return a.hash && !document.getElementById(decodeURIComponent(a.hash.slice(1))); }
        catch (_) { return true; }
      }).length
    }; });
    if (failures.size || documentState.brokenImages || documentState.brokenFonts || documentState.brokenLinks)
      throw new Error('PDF resources or internal links failed: ' + JSON.stringify({
        resources: Array.from(failures), images: documentState.brokenImages,
        fonts: documentState.brokenFonts, links: documentState.brokenLinks}));
    await page.pdf({path: jobs.output, format: jobs.page_size, printBackground: true,
      margin: {top: jobs.margin, bottom: jobs.margin, left: jobs.margin, right: jobs.margin},
      displayHeaderFooter: jobs.page_numbers, headerTemplate: '<span></span>',
      footerTemplate: '<div style="font-size:9px;text-align:center;width:100%"><span class="pageNumber"></span></div>',
      timeout: 120000});
    fs.writeFileSync(jobs.text, documentState.text, 'utf8');
    fs.writeFileSync(jobs.entries, JSON.stringify(documentState.entries), 'utf8');
    console.log(JSON.stringify({browser: await browser.version(),
      puppeteer: require('puppeteer/package.json').version}));
  } finally { await browser.close(); }
})().catch(e => { console.error(String(e && e.message || e)); process.exit(1); });
"""


def requirements():
    """Fail before preparation if optional PDF dependencies are absent."""
    try:
        import pypdf  # noqa: F401
    except ImportError:
        raise BuildError('PDF needs pypdf: install mdbindery[pdf] (from source: pip install ".[pdf]")')
    try:
        return tools.puppeteer_runtime()
    except RuntimeError as e:
        raise BuildError(f'PDF export: {e}') from e


def _tokens(text):
    text = unicodedata.normalize('NFKC', text).replace('\u00ad', '')
    return Counter(re.findall(r"[^\W_][\w'’-]*", text))


def validate_pdf(path, expected, opts):
    """Read the actual PDF and compare text with the rendered HTML, independent of page order."""
    from pypdf import PdfReader
    try:
        with Path(path).open('rb') as stream:
            reader = PdfReader(stream, strict=True)
            count = len(reader.pages)
            if not count or reader.is_encrypted:
                raise ValueError('empty or encrypted PDF')
            text = '\n'.join(p.extract_text() or '' for p in reader.pages)
    except Exception as e:
        raise BuildError(f'PDF cannot be read: {e}') from e
    source, output = _tokens(expected), _tokens(text)
    missing = sum((source - output).values())
    total = sum(source.values())
    limit = max(opts['wordcount_min_words'], total * opts['wordcount_tolerance'])
    ok = bool(source) and any(any(c.isalpha() for c in token) for token in output) and missing <= limit
    return {'result': 'pass' if ok else 'fail', 'pages': count, 'html_words': total,
            'pdf_words': sum(output.values()), 'missing_words': missing, 'tolerance_words': limit}


def render(cfg, analysis, work, css, cover, destination, date, log, *, runtime):
    """Stage only declared assets, print one HTML document, validate, then atomically publish PDF."""
    node, env = runtime
    settings = cfg.opts['pdf']
    document = json.loads(Path(analysis['linked']).read_text(encoding='utf-8'))
    meta = {k: cfg.meta[k] for k in ('title', 'subtitle', 'lang', 'description')}
    meta.update(author=cfg.meta['authors'], date=date)
    toc_title = config.toc_title(cfg.opts) if cfg.opts['toc'] else ''
    if toc_title:
        meta['toc-title'] = toc_title   # pandoc's HTML template puts it in nav#TOC; a literal string, never a boolean
    document['meta'] = {k: meta_value(v) for k, v in meta.items()}
    assets = work / 'pdf-assets'
    assets.mkdir(exist_ok=True)
    staged = {}

    def asset(source):
        source = Path(source).resolve()
        # Recheck untrusted paths immediately before copying. Generated assets live in work.
        if not cfg.trusted and not (config.contained(source, cfg.repo_root) or config.contained(source, work)):
            raise BuildError('PDF asset points outside the repository')
        if not source.is_file():
            raise BuildError(f'PDF asset not found: {source}')
        if source not in staged:
            name = hashlib.sha256(str(source).encode('utf-8')).hexdigest()[:20] + source.suffix.lower()
            target = assets / name
            shutil.copyfile(source, target)
            staged[source] = target
        return staged[source]

    def walk(value):
        if isinstance(value, dict):
            if value.get('t') == 'Image':
                value['c'][2][0] = asset(value['c'][2][0]).as_uri()
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(document)
    cover_path = asset(cover)
    # Font files are copied only when explicitly listed; CSS never triggers arbitrary file reads.
    fonts = {}
    for name in cfg.opts['embed_fonts']:
        original = Path(name)
        source = original if original.is_absolute() else cfg.base / original
        target = asset(source)
        for key in (name, original.as_posix(), source.resolve().as_uri(), str(source.resolve())):
            fonts[key] = target.as_uri()
    css_text = css.read_text(encoding='utf-8')
    css_text = re.sub(r'url\(\s*([\'"]?)(.*?)\1\s*\)',
                      lambda m: 'url("' + fonts[m[2]] + '")' if m[2] in fonts else m[0], css_text)
    pdf_css = work / 'pdf-book.css'
    pdf_css.write_text(css_text, encoding='utf-8')
    print_css = work / 'print.css'
    shutil.copyfile(DATA / 'print.css', print_css)
    # Bound image height by printable page dimensions, including full-page figures.
    page_height = 297 if settings['page_size'] == 'A4' else 279.4
    with print_css.open('a', encoding='utf-8') as stream:
        stream.write(f'\n:root {{ --page-height: {page_height - 2 * settings["margin_mm"]}mm; }}\n')
    front = work / 'pdf-cover.html'
    front.write_text('<section class="pdf-cover"><img src="' + html.escape(cover_path.as_uri(), quote=True)
                     + '" alt="' + html.escape(cfg.meta['title'], quote=True) + '"></section>', encoding='utf-8')
    book = work / 'pdf-book.json'
    book.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    html_file = work / 'book.html'
    command = [tools.find('pandoc'), '-f', 'json', '-t', 'html5', '--standalone', '--mathml', book,
               '-o', html_file, '--css', pdf_css.as_uri(), '--css', print_css.as_uri(),
               '--include-before-body', front, '--toc-depth', str(cfg.opts['toc_depth']),
               '--syntax-highlighting=' + (cfg.opts['highlight_style'] or 'none')]
    if cfg.opts['toc']:
        command.append('--toc')
    result = run(command, timeout=1800)
    for line in result.stderr.splitlines():
        log(f'  pandoc: {line}')
    rights = cfg.meta.get('rights') or ''
    entries, has_nav = pdfnav.prepare(html_file, rights)   # anchors, locators and the rights line
    script = work / 'pdf.js'
    script.write_text(JS, encoding='utf-8')
    text_file = work / 'pdf-text.txt'
    entries_file = work / 'pdf-entries.json'
    temporary = destination.with_name(f'{destination.name}.{os.getpid()}.part')
    final = destination.with_name(f'{destination.name}.{os.getpid()}.final.part')
    # Explicit opt-out only. A failed sandbox or remembered Mermaid fallback never downgrades PDF silently.
    args = ['--no-sandbox'] if os.environ.get('MDBINDERY_NO_SANDBOX') else []
    if args:
        log('  warning: PDF browser sandbox disabled by MDBINDERY_NO_SANDBOX')
    jobs = {'html': str(html_file), 'output': str(temporary), 'text': str(text_file), 'entries': str(entries_file),
            'files': [str(p) for p in [html_file, pdf_css, print_css, *staged.values()]],
            'args': args, 'page_size': settings['page_size'], 'page_numbers': settings['page_numbers'],
            'margin': f'{settings["margin_mm"]}mm'}
    jobs_file = work / 'pdf-jobs.json'
    jobs_file.write_text(json.dumps(jobs), encoding='utf-8')
    meta = {k: cfg.meta.get(k) or ('' if k not in ('authors', 'subjects') else []) for k in
            ('title', 'authors', 'description', 'subjects', 'rights', 'publisher', 'lang')}
    try:
        result = run([node, script, jobs_file], env=env, timeout=600, tail=65536)
        browser = json.loads(result.stdout.strip())
        entries = pdfnav.join_entries(entries, json.loads(entries_file.read_text(encoding='utf-8')))
        try:
            from pypdf import PdfReader, __version__ as pypdf_version
            rows, _ = pdfnav.project(PdfReader(temporary, strict=True), entries, has_nav, rights)
            pdfnav.finalize(temporary, final, rows, meta, aliases={e['target']: e['anchor'] for e in entries})
        except BuildError:
            raise
        except Exception as e:   # a parser or writer failure: the previous PDF stays
            raise BuildError(f'PDF navigation and metadata could not be written: {e}') from e
        check = validate_pdf(final, text_file.read_text(encoding='utf-8'), cfg.opts)
        outline, front_matter, metadata = pdfnav.gates(final, rows, meta, toc_title, has_nav, rights,
                                                       printed=[e['text'] for e in entries], listed=[e['words'] for e in entries],
                                                       links={e['target']: row[2] for e, row in zip(entries, rows)})
        final.replace(destination)   # a failed gate keeps the file as a diagnostic, as the word gate always has
    finally:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
    browser['pypdf'] = pypdf_version
    log(f'  PDF: {check["pages"]} pages; {check["missing_words"]} missing words from rendered HTML; '
        f'outline {outline["result"]} ({len(rows)} entries), front matter {front_matter["result"]}, metadata {metadata["result"]}')
    gate = dict(check, words=check['result'], outline=outline, front_matter=front_matter, metadata=metadata)   # each part on its own
    gate['result'] = 'pass' if all(r == 'pass' for r in (check['result'], outline['result'], front_matter['result'],
                                                          metadata['result'])) else 'fail'
    return gate, browser
