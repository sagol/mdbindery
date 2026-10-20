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
    const documentState = await page.evaluate(locators => {
      const source = document.body.cloneNode(true);
      source.querySelectorAll('annotation, script, style').forEach(n => n.remove());
      const hide = id => {   // Chrome writes a destination only for a link target, and finds the links in the whole document:
        const link = document.createElement('a');   // these go in the head, which is never rendered; a selector that sees
        link.className = 'mdb-locator';             // them (:has() on html or head) could still change the print, so the page
                                                    // is printed again without them and the two must match
        link.setAttribute('href', '#' + id);
        link.style.display = 'none';
        document.head.append(link);
      };
      const collapsed = style => {   // a transform, or the scale property, that leaves what it draws no area (scale(0),
        const t = style.transform;   // scaleX(0) rotated, scale: 0 1)
        if (t && t !== 'none') {
          const m = ((/\(([^()]*)\)/.exec(t) || [, ''])[1]).split(',').map(Number);   // the arguments, not the 3 in matrix3d
          const [a, b, c, d] = m.length === 16 ? [m[0], m[1], m[4], m[5]] : m;
          if (Math.abs(a * d - b * c) < 1e-9) return true;
        }
        const [x, y = x] = style.scale && style.scale !== 'none' ? style.scale.split(/\s+/).map(parseFloat) : [1];
        return Math.abs(x * y) < 1e-9;
      };
      const flat = el => {   // on el or above it
        for (let e = el; e; e = e.parentElement) if (collapsed(getComputedStyle(e))) return true;
        return false;
      };
      const place = el => {   // where Chrome can write el's destination: el itself when it has a box, else the first element
        if (!el) return null;   // in it with a box and an id of its own (display: contents gives none). Nothing is inserted
        const boxed = e => getComputedStyle(e).display !== 'contents' && e.getClientRects().length > 0;   // and no attribute is
        return boxed(el) ? el : Array.from(el.querySelectorAll('[id]')).find(boxed) || null;   // set, so no selector of the
      };   // book's CSS matches differently
      const places = {};   // the title block, the contents nav and the rights line: the element each destination is written for
      for (const ident of locators) {
        const found = place(document.getElementById(ident));
        if (found) {
          places[ident] = found.id;
          hide(found.id);
        }
      }
      const alpha = color => {   // a computed colour's alpha, from its own arguments: after a slash (rgb(… / a), color(space … / a),
        if (color === 'transparent') return 0;   // oklch(… / a)), or the fourth of rgba(r, g, b, a); none of those is opaque
        const args = /\(([^()]*)\)\s*$/.exec(color);
        const value = !args ? null : args[1].includes('/') ? args[1].split('/').pop()
          : args[1].split(',').length === 4 ? args[1].split(',')[3] : null;
        if (value === null) return 1;
        const a = value.trim();
        return a === 'none' ? 0 : parseFloat(a) / (a.endsWith('%') ? 100 : 1);
      };
      const scan = root => {   // what root prints: its label (an image by its alt text), its words (no alt
        let label = '', words = '', shows = false, outlined = false, runs = [];   // text), whether anything shows, whether text is drawn
        // only as an outline or a shadow (its extracted text cannot be told), and generated text no page can repeat;
        const unresolved = [];   // ::before and ::after included
        const cased = (piece, style, el) => {   // as text-transform prints it: Chrome's own result, read from a probe in the
          if (!style || ['none', 'math-auto'].includes(style.textTransform) || !el) return piece;   // element (its language and
          // context), forced to show the transform; math-auto only italicizes a math letter, as a style, so it stays plain
          const read = text => {   // the probe shows the transform, keeps spaces, starts its own line context and is visible
            const probe = document.createElement('span');   // whatever the book's CSS or its element says (a hidden probe
            probe.textContent = text;   // reads as nothing); it leaves before printing
            for (const [k, v] of [['text-transform', style.textTransform], ['display', 'inline-block'], ['white-space', 'pre'],
                                  ['visibility', 'visible'], ['content-visibility', 'visible']])
              probe.style.setProperty(k, v, 'important');
            el.appendChild(probe);
            const out = probe.innerText;
            probe.remove();
            return out;
          };
          const tail = /\bcapitalize\b/.test(style.textTransform) && /[\p{L}\p{N}]$/u.test(label) ? Array.from(label).pop() : '';
          const out = tail ? read(tail + piece).slice(read(tail).length) : read(piece);   // a word the piece before began goes
          return piece.trim() && !out.trim() ? piece : out;   // on after a letter or digit (Chrome starts a new one after a
        };   // combining mark); a probe that reads nothing never erases what the element prints
        const add = (piece, alt, outline = false, style = null, el = null) => {   // runs: its printed text in order, each piece
          piece = cased(piece, style, el);   // with whether it is drawn only as an outline (Chrome writes such a piece twice)
          label += piece;
          if (!alt) words += piece;
          if (!alt || outline) runs.push([piece, outline]);
        };
        const inks = (el, style) => {   // its text is drawn: a visible fill colour, or a visible stroke or shadow around a clear
          const shadows = style.textShadow === 'none' ? [] : style.textShadow.match(/(?:rgba?|hsla?|color|oklch|oklab|lab|lch)\([^()]*\)|transparent/g) || [];
          const painted = alpha(style.webkitTextFillColor) > 0 || parseFloat(style.webkitTextStrokeWidth) > 0   // fill (a shadow
            && alpha(style.webkitTextStrokeColor) > 0 || shadows.some(c => alpha(c) > 0);   // counts by its colour); no zero
          // opacity on it or above it, and no
          if (!painted || flat(el)) return false;   // transform that leaves it no area, nor a filter that zeroes its opacity
          for (let e = el; e; e = e.parentElement) if (/opacity\(\s*0*(?:\.0*)?%?\s*\)/.test(getComputedStyle(e).filter)) return false;
          for (let e = el; e; e = e.parentElement) if (parseFloat(getComputedStyle(e).opacity) === 0) return false;
          return parseFloat(style.opacity) !== 0;
        };
        const generated = (el, which) => {   // the words a visible ::before or ::after prints: its strings and attr() values;
          const style = getComputedStyle(el, which);   // a counter or a quote mark is not readable here, so it is reported
          const value = style.content;
          if (!value || value === 'none' || value === 'normal' || style.display === 'none' || style.visibility !== 'visible'
              || !inks(el, style)) return;
          if (parseFloat(style.fontSize) === 0 || style.clipPath !== 'none' || (style.maskImage || style.webkitMaskImage || 'none')
              !== 'none' || collapsed(style) || masked(el)   // the pseudo-element's own drawing: no size, its own clip or mask,
              || /opacity\(\s*0*(?:\.0*)?%?\s*\)/.test(style.filter)) return;   // a collapsing transform, a filter that clears it
          let box = el;   // where its text starts: in its element (or the nearest one above with a box), at least a line
          while (box && !box.getClientRects().length) box = box.parentElement;   // high and a letter wide, since an element
          const r = box && box.getBoundingClientRect(), size = parseFloat(style.fontSize);   // of no height still prints its
          if (r && !onPage({left: r.left, top: r.top, right: Math.max(r.right, r.left + size),   // overflow; then clipped by it
                            bottom: Math.max(r.bottom, r.top + (parseFloat(style.lineHeight) || 1.2 * size))}, el)) return;   // and above
          const offset = ['top', 'right', 'bottom', 'left'].some(k => !['auto', '0px'].includes(style[k]));   // neither
          if (['absolute', 'fixed'].includes(style.position) || style.position !== 'static' && offset
              || ['transform', 'translate', 'rotate', 'scale'].some(k => style[k] && style[k] !== 'none')) {   // any transform, the
            // identity included, paints it in a layer of its own, whose text Chrome writes after the page's flow
            unresolved.push(`${which} moved by position: ${style.position} or a transform`);   // moved apart from the flow,
            return;   // where it lands cannot be told here (a pseudo-element has no box to measure)
          }
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
          outlined = outlined || alpha(style.webkitTextFillColor) === 0;   // an outline or a shadow only: no words to find
          add(printed, alpha(style.webkitTextFillColor) === 0, alpha(style.webkitTextFillColor) === 0, style, el);
        };
        const hides = el => {   // nothing inside it prints, its ::before and ::after included, whatever their own style says
          const style = getComputedStyle(el);
          return style.display === 'none' || style.contentVisibility === 'hidden';
        };
        const walk = node => {
          if (node.nodeType === Node.ELEMENT_NODE) generated(node, '::before');
          for (const n of node.childNodes) {
            if (n.nodeType === Node.TEXT_NODE) {
              if (!n.data.trim()) {   // a space that wraps a line may have no box of its own; one in an element of no size
                if (parseFloat(getComputedStyle(n.parentElement).fontSize) > 0) add(' ');   // prints nothing between its
                continue;   // neighbours
              }
              const range = document.createRange(), style = getComputedStyle(n.parentElement);
              range.selectNodeContents(n);   // its text draws: a box with an area on the page, visible, inked, not clipped away
              if (Array.from(range.getClientRects()).some(box => onPage(box, n.parentElement)) && style.visibility === 'visible'
                  && inks(n.parentElement, style) && !masked(n.parentElement)) {
                outlined = outlined || alpha(style.webkitTextFillColor) === 0;   // only a stroke or a shadow: Chrome may write its
                add(n.data, alpha(style.webkitTextFillColor) === 0, alpha(style.webkitTextFillColor) === 0, style, n.parentElement);
                shows = true;
              }
            } else if (n.nodeType === Node.ELEMENT_NODE && !['annotation', 'script', 'style'].includes(n.localName) && !hides(n)) {
              if (n.localName === 'br') add(' ');
              else if (n.localName !== 'img') walk(n);
              else if (drawn(n)) {
                add(' ' + (n.getAttribute('alt') || '') + ' ', true);
                shows = true;
              }
            }
          }
          if (node.nodeType === Node.ELEMENT_NODE) generated(node, '::after');
        };
        const masked = el => {   // a clip path, a mask or a clip rectangle on el or above it: what it leaves cannot be told,
          for (let e = el; e; e = e.parentElement) {   // so what is under it counts as not drawn
            const style = getComputedStyle(e);
            if (style.clipPath !== 'none' || (style.maskImage || style.webkitMaskImage || 'none') !== 'none'
                || (style.clip !== 'auto' && ['absolute', 'fixed'].includes(style.position))) return true;
          }
          return false;
        };
        const onPage = (box, el) => {   // what is left of box after every ancestor that clips its overflow (one with a box that
          for (let e = el; e; e = e.parentElement) {   // is not inline) has an area and lies within the page's printable width
            const style = getComputedStyle(e);
            if ((style.overflowX !== 'visible' || style.overflowY !== 'visible') && !['contents', 'inline'].includes(style.display)) {
              const outer = e.getBoundingClientRect();
              box = {left: Math.max(box.left, outer.left), top: Math.max(box.top, outer.top),
                     right: Math.min(box.right, outer.right), bottom: Math.min(box.bottom, outer.bottom)};
            }
          }
          return box.right > box.left && box.bottom > box.top && box.right > 0 && box.bottom > 0
            && box.left < document.documentElement.clientWidth;
        };
        const filtered = el => {   // a filter on it or above that clears it (opacity(0)), or an SVG filter whose result cannot be
          for (let e = el; e; e = e.parentElement)   // told; grayscale, blur and the like still print it
            if (/url\(|opacity\(\s*0*(?:\.0*)?%?\s*\)/.test(getComputedStyle(e).filter)) return true;
          return false;
        };
        const inside = img => {   // its replaced content overlaps its own box, after object-fit and object-position; a
          const s = getComputedStyle(img), bw = img.clientWidth, bh = img.clientHeight;   // position that cannot be read
          const nw = img.naturalWidth, nh = img.naturalHeight;   // (calc()) counts as outside
          if (s.objectFit === 'fill' || !nw || !nh) return true;
          const k = {contain: Math.min(bw / nw, bh / nh), cover: Math.max(bw / nw, bh / nh), none: 1,
                     'scale-down': Math.min(1, bw / nw, bh / nh)}[s.objectFit] ?? 1;
          const [px, py = px] = s.objectPosition.split(/\s+/);
          const at = (v, free) => v.endsWith('%') ? free * parseFloat(v) / 100 : parseFloat(v);
          const x = at(px, bw - nw * k), y = at(py, bh - nh * k);
          return x < bw && x + nw * k > 0 && y < bh && y + nh * k > 0;
        };
        const drawn = img => img.checkVisibility({visibilityProperty: true, opacityProperty: true}) && !flat(img) && !masked(img)
          && !filtered(img) && inside(img) && onPage(img.getBoundingClientRect(), img.parentElement);   // an image draws: visible, not
          // transparent, not filtered away (its alt text is no text to find), an area left
        let lays = true;   // root and every ancestor lay out
        for (let el = root; el; el = el.parentElement) if (hides(el)) lays = false;
        if (lays) walk(root);
        return {label: label.replace(/\s+/g, ' ').trim(), words: words.replace(/\s+/g, ' ').trim(), shows, unresolved, outlined,
                runs};
      };
      const marker = a => {   // the text its list item's marker prints before the link: a counter in the list's style, a
        const li = a.closest('li');   // ::marker string, nothing for a bullet or a marker Chrome does not write; null where it
        if (!li || getComputedStyle(li).display !== 'list-item' || li.closest('nav#TOC') === null) return '';   // cannot be told
        const drawn = getComputedStyle(li, '::marker');   // hidden or of no size: Chrome writes nothing before it (a clear
        if (drawn.visibility !== 'visible' || parseFloat(drawn.fontSize) === 0) return '';   // colour still leaves its text)
        const content = drawn.content;
        if (content && content !== 'normal') {
          if (content === 'none') return '';
          return /counter|quote|attr/.test(content) ? null : (content.match(/"((?:[^"\\]|\\.)*)"/g) || []).map(q => q.slice(1, -1)).join('');
        }
        const counted = e => { const st = getComputedStyle(e);   // counters the book's CSS sets: the ordinal is no longer the place
          return [st.counterIncrement, st.counterSet].some(v => v && v !== 'none') || !['none', 'list-item 0'].includes(st.counterReset); };
        if (Array.from(li.parentElement.children).some(c => c.localName === 'li' && counted(c)) || counted(li.parentElement)) return null;
        const type = getComputedStyle(li).listStyleType, n = Array.from(li.parentElement.children).filter(c => c.localName === 'li').indexOf(li) + 1;
        const letters = k => { let out = ''; while (k > 0) { k -= 1; out = String.fromCharCode(97 + k % 26) + out; k = Math.floor(k / 26); } return out; };
        const roman = k => [[1000, 'm'], [900, 'cm'], [500, 'd'], [400, 'cd'], [100, 'c'], [90, 'xc'], [50, 'l'], [40, 'xl'], [10, 'x'],
          [9, 'ix'], [5, 'v'], [4, 'iv'], [1, 'i']].reduce((out, [v, r]) => { while (k >= v) { out += r; k -= v; } return out; }, '');
        if (['none', 'disc', 'circle', 'square', 'disclosure-open', 'disclosure-closed'].includes(type)) return '';
        if (type === 'decimal') return String(n);
        if (type === 'decimal-leading-zero') return n < 10 ? '0' + n : String(n);
        if (/^(lower|upper)-(alpha|latin)$/.test(type)) return letters(n);
        if (/^(lower|upper)-roman$/.test(type)) return roman(n);
        return null;
      };
      const nav = document.getElementById('TOC');
      let before = '';   // what is drawn before the contents nav (the title block, the cover): a page the two share starts with it
      for (const el of nav ? document.body.children : []) {
        if (el === nav || el.contains(nav)) break;
        before += ' ' + scan(el).words;
      }
      const element = selector => {   // a front-matter element as printed: whether anything of it draws, and the words it
        const el = document.querySelector(selector);   // draws, generated text included
        if (!el) return null;
        const seen = scan(el);   // its words here include text drawn only as an outline or a shadow, which is visible
        return {shows: seen.shows, words: seen.runs.map(r => r[0]).join('')};   // (an image's alt text is not)
      };
      const front = {title: element('header#title-block-header .title'), rights: element('header#title-block-header p.rights'),
                     contents: element('nav#TOC > h2, nav#TOC > h1, nav#TOC > header')};
      const entries = Array.from(document.querySelectorAll('nav#TOC a[href^="#"]')).map((a, n) => {   // in the order prepare counted
        const id = `mdb-dest-${n + 1}`;   // them: the n-th contents link is entry n, marked by nothing in the document
        let target = null;
        try { target = document.getElementById(decodeURIComponent(a.getAttribute('href').slice(1))); } catch (_) {}
        const anchor = place(target);   // the heading itself, or for one with no box the first element in it with a box and an id
        if (anchor) hide(anchor.id);
        const name = a.cloneNode(true);   // the entry's full text, an image by its alt text, to name it in a message
        name.querySelectorAll('annotation, script, style').forEach(n => n.remove());
        name.querySelectorAll('br, img').forEach(n => n.replaceWith(' ' + (n.getAttribute('alt') || '') + ' '));
        const entry = scan(a), heading = target ? scan(target) : {words: '', shows: false};   // what the heading draws, its
        return {id, anchor: anchor ? anchor.id : null, label: entry.label, words: entry.words,   // ::before and ::after included
          text: heading.words, name: name.textContent.replace(/\s+/g, ' ').trim(), listed: entry.shows,
          unresolved: entry.unresolved, marker: marker(a), outlined: entry.outlined, runs: entry.outlined ? entry.runs : null,
          printed: !!anchor && anchor.checkVisibility() && heading.shows,   // laid out, and something in it drawn: a visible part
          // of a hidden heading, or text it generates, prints, and Chrome writes the heading's destination
          placeless: !anchor && !!target && getComputedStyle(target).display === 'contents'};
      });
      const links = source.querySelectorAll('nav#TOC a[href^="#"]');   // the word count expects of the contents only the words
      entries.forEach((e, n) => links[n] && links[n].replaceWith(` ${e.words} `));   // each entry prints, not those CSS hides
      return {
      text: source.textContent,
      places,
      before,
      front,
      entries,
      brokenImages: Array.from(document.images).filter(i => !i.complete || !i.naturalWidth).length,
      brokenFonts: Array.from(document.fonts).filter(f => f.status === 'error').length,
      brokenLinks: Array.from(document.querySelectorAll('a[href^="#"]')).filter(a => {
        try { return a.hash && !document.getElementById(decodeURIComponent(a.hash.slice(1))); }
        catch (_) { return true; }
      }).length
    }; }, jobs.locators);
    if (failures.size || documentState.brokenImages || documentState.brokenFonts || documentState.brokenLinks)
      throw new Error('PDF resources or internal links failed: ' + JSON.stringify({
        resources: Array.from(failures), images: documentState.brokenImages,
        fonts: documentState.brokenFonts, links: documentState.brokenLinks}));
    const print = path => page.pdf({path, format: jobs.page_size, printBackground: true,
      margin: {top: jobs.margin, bottom: jobs.margin, left: jobs.margin, right: jobs.margin},
      displayHeaderFooter: jobs.page_numbers, headerTemplate: '<span></span>',
      footerTemplate: '<div style="font-size:9px;text-align:center;width:100%"><span class="pageNumber"></span></div>',
      timeout: 120000});
    await print(jobs.output);
    await page.evaluate(() => document.head.querySelectorAll('a.mdb-locator').forEach(n => n.remove()));
    await print(jobs.reference);   // the book as its own CSS prints it, with nothing of mdbindery's in the head
    fs.writeFileSync(jobs.text, documentState.text, 'utf8');
    fs.writeFileSync(jobs.entries, JSON.stringify({entries: documentState.entries, places: documentState.places,
      before: documentState.before, front: documentState.front}), 'utf8');
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
    entries, has_nav = pdfnav.prepare(html_file, rights)   # the contents entries, and the rights line
    script = work / 'pdf.js'
    script.write_text(JS, encoding='utf-8')
    text_file = work / 'pdf-text.txt'
    entries_file = work / 'pdf-entries.json'
    temporary = destination.with_name(f'{destination.name}.{os.getpid()}.part')
    final = destination.with_name(f'{destination.name}.{os.getpid()}.final.part')
    reference = destination.with_name(f'{destination.name}.{os.getpid()}.reference.part')
    # Explicit opt-out only. A failed sandbox or remembered Mermaid fallback never downgrades PDF silently.
    args = ['--no-sandbox'] if os.environ.get('MDBINDERY_NO_SANDBOX') else []
    if args:
        log('  warning: PDF browser sandbox disabled by MDBINDERY_NO_SANDBOX')
    jobs = {'html': str(html_file), 'output': str(temporary), 'reference': str(reference), 'text': str(text_file), 'entries': str(entries_file),
            'files': [str(p) for p in [html_file, pdf_css, print_css, *staged.values()]],
            'args': args, 'page_size': settings['page_size'], 'page_numbers': settings['page_numbers'],
            'margin': f'{settings["margin_mm"]}mm', 'locators': [pdfnav.TITLE, pdfnav.CONTENTS] + ([pdfnav.RIGHTS] if rights else [])}
    jobs_file = work / 'pdf-jobs.json'
    jobs_file.write_text(json.dumps(jobs), encoding='utf-8')
    meta = {k: cfg.meta.get(k) or ('' if k not in ('authors', 'subjects') else []) for k in
            ('title', 'authors', 'description', 'subjects', 'rights', 'publisher', 'lang')}
    try:
        result = run([node, script, jobs_file], env=env, timeout=600, tail=65536)
        browser = json.loads(result.stdout.strip())
        reported = json.loads(entries_file.read_text(encoding='utf-8'))   # each entry as printed, and each locator's element
        entries, places = pdfnav.join_entries(entries, reported['entries']), reported['places']
        try:
            from pypdf import PdfReader, __version__ as pypdf_version
            changed = pdfnav.changed_page(temporary, reference)
            if changed is not None:
                raise BuildError('PDF: the book\'s CSS prints differently when mdbindery adds its hidden navigation links to '
                                 'the document head (' + (f'page {changed}' if changed else 'the page count') + '): a selector '
                                 'sees them, such as :has() on html or head, or a sibling selector there; remove it')
            rows, _ = pdfnav.project(PdfReader(temporary, strict=True), entries, has_nav, rights, places)
            pdfnav.finalize(temporary, final, rows, meta, aliases={e['target']: e['anchor'] for e in entries})
        except BuildError:
            raise
        except Exception as e:   # a parser or writer failure: the previous PDF stays
            raise BuildError(f'PDF navigation and metadata could not be written: {e}') from e
        check = validate_pdf(final, text_file.read_text(encoding='utf-8'), cfg.opts)
        outline, front_matter, metadata = pdfnav.gates(final, rows, meta, toc_title, has_nav, rights, places=places,
                                                       printed=[e['text'] for e in entries], listed=[e['words'] for e in entries],
                                                       markers=[e['marker'] for e in entries],
                                                       runs=[e.get('runs') for e in entries],
                                                       before=reported['before'],
                                                       page_numbers=settings['page_numbers'], front=reported['front'],
                                                       links={e['target']: row[2] for e, row in zip(entries, rows)})
        final.replace(destination)   # a failed gate keeps the file as a diagnostic, as the word gate always has
    finally:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        reference.unlink(missing_ok=True)
    browser['pypdf'] = pypdf_version
    log(f'  PDF: {check["pages"]} pages; {check["missing_words"]} missing words from rendered HTML; '
        f'outline {outline["result"]} ({len(rows)} entries), front matter {front_matter["result"]}, metadata {metadata["result"]}')
    gate = dict(check, words=check['result'], outline=outline, front_matter=front_matter, metadata=metadata)   # each part on its own
    gate['result'] = 'pass' if all(r == 'pass' for r in (check['result'], outline['result'], front_matter['result'],
                                                          metadata['result'])) else 'fail'
    return gate, browser
