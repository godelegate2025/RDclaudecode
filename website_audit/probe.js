/*
 * Runs inside the page. Collects everything the analysis needs in one pass:
 * typography per visible text run, colour usage, effective backgrounds for
 * contrast, and the structural/meta facts we score on.
 */
() => {
  const MAX_TEXT_RUNS = 4000;

  const parseColor = (str) => {
    if (!str) return null;
    const m = str.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const parts = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    if (parts.length < 3 || parts.some(Number.isNaN)) return null;
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };

  const over = (top, bottom) => ({
    r: Math.round(top.r * top.a + bottom.r * (1 - top.a)),
    g: Math.round(top.g * top.a + bottom.g * (1 - top.a)),
    b: Math.round(top.b * top.a + bottom.b * (1 - top.a)),
    a: 1,
  });

  const hex = (c) =>
    '#' + [c.r, c.g, c.b].map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');

  /* Walk ancestors, blending translucent layers, until an opaque one is hit. */
  const effectiveBackground = (el) => {
    const layers = [];
    let node = el;
    let behindImage = false;
    while (node && node.nodeType === 1) {
      const s = getComputedStyle(node);
      if (s.backgroundImage && s.backgroundImage !== 'none') behindImage = true;
      const c = parseColor(s.backgroundColor);
      if (c && c.a > 0) {
        layers.push(c);
        if (c.a >= 1) break;
      }
      node = node.parentElement;
    }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
    return { color: base, behindImage };
  };

  /* What is painted beneath this element at its centre point? Only answerable for
     elements inside the viewport; off-screen text falls back to the ancestor walk. */
  const mediaBehind = (el, rect) => {
    if (rect.bottom <= 0 || rect.top >= window.innerHeight) return false;
    const x = Math.min(Math.max(rect.left + rect.width / 2, 1), window.innerWidth - 1);
    const y = Math.min(Math.max(rect.top + rect.height / 2, 1), window.innerHeight - 1);
    let stack;
    try {
      stack = document.elementsFromPoint(x, y);
    } catch (e) {
      return false;
    }
    if (!stack || !stack.length) return false;
    const index = stack.indexOf(el);
    /* Not in its own hit-stack means something is painted over it — also unmeasurable. */
    if (index === -1) return true;
    for (const node of stack.slice(index + 1)) {
      if (/^(img|video|canvas|svg|picture)$/.test(node.tagName.toLowerCase())) return true;
      const s = getComputedStyle(node);
      if (s.backgroundImage && s.backgroundImage !== 'none') return true;
    }
    return false;
  };

  const isVisible = (el, style) =>
    style.visibility !== 'hidden' &&
    style.display !== 'none' &&
    parseFloat(style.opacity || '1') > 0.05 &&
    el.getClientRects().length > 0;

  const ownText = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.replace(/\s+/g, ' ').trim();
  };

  const px = (v) => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : null;
  };

  const textRuns = [];
  const colorUsage = {};
  const bump = (bucket, key, amount) => {
    if (!key) return;
    colorUsage[key] = colorUsage[key] || { text: 0, background: 0, border: 0 };
    colorUsage[key][bucket] += amount;
  };

  const all = document.querySelectorAll('body *');
  let overflowing = [];
  const docWidth = document.documentElement.clientWidth;

  for (const el of all) {
    const style = getComputedStyle(el);
    if (!isVisible(el, style)) continue;
    const rect = el.getBoundingClientRect();

    if (rect.width > 0 && rect.right > docWidth + 2 && rect.width < docWidth * 3) {
      if (overflowing.length < 12) {
        overflowing.push({
          tag: el.tagName.toLowerCase(),
          cls: (el.className && typeof el.className === 'string' ? el.className : '').slice(0, 60),
          right: Math.round(rect.right),
        });
      }
    }

    const bgc = parseColor(style.backgroundColor);
    if (bgc && bgc.a > 0.05) bump('background', hex(bgc), Math.round(rect.width * rect.height));

    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      if (px(style['border' + side + 'Width']) > 0 && style['border' + side + 'Style'] !== 'none') {
        const bc = parseColor(style['border' + side + 'Color']);
        if (bc && bc.a > 0.05) bump('border', hex(bc), 1);
      }
    }

    const text = ownText(el);
    if (!text) continue;

    const fg = parseColor(style.color);
    if (!fg) continue;
    const blended = fg.a < 1 ? over(fg, effectiveBackground(el).color) : fg;
    bump('text', hex(blended), text.length);

    if (textRuns.length >= MAX_TEXT_RUNS) continue;
    const bg = effectiveBackground(el);
    const size = px(style.fontSize) || 16;
    const weight = parseInt(style.fontWeight, 10) || 400;
    const lh = style.lineHeight === 'normal' ? null : px(style.lineHeight);

    textRuns.push({
      tag: el.tagName.toLowerCase(),
      chars: text.length,
      sample: text.slice(0, 120),
      font_family: style.fontFamily,
      font_size: Math.round(size * 100) / 100,
      font_weight: weight,
      line_height: lh ? Math.round(lh * 100) / 100 : null,
      letter_spacing: style.letterSpacing === 'normal' ? 0 : px(style.letterSpacing),
      text_transform: style.textTransform,
      color: hex(blended),
      background: hex(bg.color),
      background_unverified: bg.behindImage || mediaBehind(el, rect),
      width: Math.round(rect.width),
      is_large_text: size >= 24 || (size >= 18.66 && weight >= 700),
    });
  }

  /* ---- fonts ---- */
  const loadedFaces = [];
  try {
    document.fonts.forEach((f) => {
      if (loadedFaces.length < 60) {
        loadedFaces.push({ family: f.family.replace(/["']/g, ''), weight: f.weight, style: f.style, status: f.status });
      }
    });
  } catch (e) { /* ignore */ }

  const faceRules = [];
  let cssVarCount = 0;
  let prefersColorScheme = false;
  let mediaQueryCount = 0;
  let inaccessibleSheets = 0;

  const scanRules = (rules) => {
    for (const rule of rules) {
      try {
        if (rule.constructor.name === 'CSSFontFaceRule' || rule.type === 5) {
          faceRules.push({
            family: (rule.style.getPropertyValue('font-family') || '').replace(/["']/g, '').trim(),
            display: (rule.style.getPropertyValue('font-display') || '').trim() || null,
            src: (rule.style.getPropertyValue('src') || '').slice(0, 200),
          });
        } else if (rule.media) {
          mediaQueryCount++;
          if (String(rule.media.mediaText).includes('prefers-color-scheme')) prefersColorScheme = true;
          if (rule.cssRules) scanRules(rule.cssRules);
        } else if (rule.style) {
          for (let i = 0; i < rule.style.length; i++) {
            if (rule.style[i].startsWith('--')) cssVarCount++;
          }
        }
      } catch (e) { /* ignore */ }
    }
  };

  for (const sheet of document.styleSheets) {
    try {
      scanRules(sheet.cssRules);
    } catch (e) {
      inaccessibleSheets++;
    }
  }

  /* ---- images ---- */
  const imgs = Array.from(document.images);
  const images = {
    total: imgs.length,
    missing_alt: imgs.filter((i) => !i.hasAttribute('alt')).length,
    empty_alt: imgs.filter((i) => i.getAttribute('alt') === '').length,
    lazy: imgs.filter((i) => i.getAttribute('loading') === 'lazy').length,
    no_dimensions: imgs.filter((i) => !i.getAttribute('width') || !i.getAttribute('height')).length,
    oversized: imgs
      .filter((i) => i.naturalWidth && i.clientWidth && i.naturalWidth > i.clientWidth * 2 && i.clientWidth > 0)
      .slice(0, 10)
      .map((i) => ({
        src: (i.currentSrc || i.src || '').split('/').pop().slice(0, 60),
        natural: i.naturalWidth,
        displayed: i.clientWidth,
      })),
    modern_format: imgs.filter((i) => /\.(webp|avif)(\?|$)/i.test(i.currentSrc || i.src || '')).length,
  };

  /* ---- interactive elements / focus styles ---- */
  const interactive = Array.from(document.querySelectorAll('a[href], button, input, select, textarea'));
  const smallTargets = interactive.filter((el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (!isVisible(el, s) || r.width === 0) return false;
    /* WCAG 2.2 exempts links that flow inline inside text. */
    if (s.display === 'inline' && el.tagName === 'A' && ownText(el).length > 0) return false;
    return r.height < 24 || r.width < 24;
  }).length;

  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .slice(0, 60)
    .map((h) => ({ level: Number(h.tagName[1]), text: ownText(h).slice(0, 80) || h.textContent.trim().slice(0, 80) }));

  const meta = (name, attr = 'name') => {
    const el = document.querySelector(`meta[${attr}="${name}"]`);
    return el ? el.getAttribute('content') : null;
  };

  /* ---- structure: navigation, footer, and the actions offered ---- */
  const linkText = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();

  const collectLinks = (selector, cap) => {
    const out = [];
    const seen = new Set();
    for (const container of document.querySelectorAll(selector)) {
      for (const a of container.querySelectorAll('a[href]')) {
        if (!/^https?:/.test(a.href)) continue;
        const text = linkText(a).slice(0, 60);
        const key = a.href + '|' + text;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ text, href: a.href });
        if (out.length >= cap) return out;
      }
    }
    return out;
  };

  const navLinks = collectLinks('nav, header, [role="navigation"]', 60);
  const footerLinks = collectLinks('footer, [role="contentinfo"]', 80);

  /* A call to action is a link or button styled to be pressed, not read. */
  const isProminent = (el) => {
    if (el.tagName === 'BUTTON') return true;
    const cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
    if (/\b(btn|button|cta)\b/.test(cls)) return true;
    const s = getComputedStyle(el);
    const bg = s.backgroundColor || '';
    const opaque = bg && !/rgba?\([^)]*,\s*0(\.0+)?\)/.test(bg) && bg !== 'transparent';
    const padded = parseFloat(s.paddingLeft) >= 10 && parseFloat(s.paddingTop) >= 6;
    return opaque && padded;
  };

  const ctas = [];
  const ctaSeen = new Set();
  for (const el of document.querySelectorAll('a[href], button')) {
    const style = getComputedStyle(el);
    if (!isVisible(el, style)) continue;
    const text = linkText(el).slice(0, 60);
    /* Carousel arrows and icon-only controls are not calls to action. */
    if (!text || text.length < 3 || text.length > 45 || !/[a-z]/i.test(text)) continue;
    if (!isProminent(el)) continue;
    const href = el.tagName === 'A' && /^https?:/.test(el.href) ? el.href : null;
    const key = text.toLowerCase() + '|' + (href || '');
    if (ctaSeen.has(key)) continue;
    ctaSeen.add(key);
    ctas.push({ text, href });
    if (ctas.length >= 40) break;
  }

  /* Machine-readable signals: schema blocks, canonical, indexability. */
  const jsonLd = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .map((el) => (el.textContent || '').slice(0, 20000))
    .filter(Boolean)
    .slice(0, 20);

  const robotsMeta = (meta('robots') || '').toLowerCase();
  const canonicalEl = document.querySelector('link[rel="canonical"]');

  const stylesheets = Array.from(document.querySelectorAll('link[rel~="stylesheet"]')).map((l) => ({
    href: l.href,
    media: l.media || 'all',
    in_head: !!l.closest('head'),
  }));

  return {
    title: document.title || null,
    body_text_sample: (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 1500),
    /* Full visible text, for the content checks. Capped so one enormous page
       cannot blow up memory on a whole-site crawl. */
    body_text: (document.body.innerText || '').replace(/[ \t]+/g, ' ').trim().slice(0, 120000),
    links: Array.from(document.querySelectorAll('a[href]'))
      .map((a) => a.href)
      .filter((h) => /^https?:/.test(h))
      .slice(0, 800),
    lang: document.documentElement.getAttribute('lang'),
    meta_description: meta('description'),
    viewport_meta: meta('viewport'),
    og_title: meta('og:title', 'property'),
    og_image: meta('og:image', 'property'),
    favicon: !!document.querySelector('link[rel~="icon"]'),
    nav_links: navLinks,
    footer_links: footerLinks,
    ctas: ctas,
    json_ld: jsonLd,
    canonical: canonicalEl ? canonicalEl.href : null,
    robots_meta: robotsMeta || null,
    noindex: /\bnoindex\b/.test(robotsMeta),
    twitter_card: meta('twitter:card'),
    hreflang_count: document.querySelectorAll('link[rel="alternate"][hreflang]').length,
    theme_color: meta('theme-color'),
    color_scheme_meta: meta('color-scheme'),
    headings,
    text_runs: textRuns,
    color_usage: colorUsage,
    loaded_faces: loadedFaces,
    face_rules: faceRules,
    css_var_count: cssVarCount,
    prefers_color_scheme: prefersColorScheme,
    media_query_count: mediaQueryCount,
    inaccessible_sheets: inaccessibleSheets,
    stylesheets,
    images,
    small_targets: smallTargets,
    interactive_count: interactive.length,
    dom_nodes: document.querySelectorAll('*').length,
    inline_style_attrs: document.querySelectorAll('[style]').length,
    doc_width: docWidth,
    scroll_width: document.documentElement.scrollWidth,
    overflowing,
    body_background: (() => {
      const b = effectiveBackground(document.body);
      return hex(b.color);
    })(),
  };
}
