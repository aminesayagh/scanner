// Runs inside the captured page via page.evaluate. Returns rendered elements in DOM order.
() => {
  const SKIP = new Set(['script', 'style', 'noscript', 'template', 'head', 'meta', 'link', 'title', 'base']);
  // Rendered as one unit: their internals are drawing primitives or another document.
  const LEAF = new Set(['svg', 'math', 'canvas', 'iframe', 'object', 'embed', 'select', 'video', 'audio']);
  const STYLE_KEYS = [
    'display', 'position', 'visibility', 'opacity', 'zIndex', 'overflowX', 'overflowY',
    'backgroundColor', 'backgroundImage',
    'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'marginTop', 'marginRight', 'marginBottom', 'marginLeft',
    'fontSize', 'lineHeight', 'flexDirection', 'gridTemplateColumns', 'gap',
  ];
  const scrollX = window.scrollX, scrollY = window.scrollY;
  const root = document.scrollingElement || document.documentElement;
  const docW = root.scrollWidth, docH = root.scrollHeight;
  const out = [];

  const rect = (r) => ({ x: r.left, y: r.top, width: r.width, height: r.height });
  const collapse = (t) => t.replace(/\s+/g, ' ').trim();
  // Where the element sits in the DOM, anchored on the nearest ancestor with an id: e.g. "#work>ul>li[3]>h3".
  // Siblings are counted per tag. Used to match the same slot across separate page loads.
  const pathOf = (el) => {
    const parts = [];
    for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
      if (n.id) { parts.unshift('#' + n.id); break; }
      let i = 0;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
      parts.unshift(n.tagName.toLowerCase() + (i ? `[${i}]` : ''));
    }
    return parts.join('>');
  };
  const ownFullText = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === Node.TEXT_NODE) t += n.textContent;
    return collapse(t);
  };
  const ownText = (el) => {
    const t = ownFullText(el);
    return t.length > 80 ? t.slice(0, 79) + '…' : t;
  };
  // Headings, paragraphs and list items are read whole (inline links and emphasis included).
  const TEXT_BLOCKS = new Set(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li']);
  const NO_TEXT = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  // The block's DOM text, including parts not painted right now (a closed <details>, content hidden until
  // an animation or click); only display: none parts are left out. Block-level children are separated by
  // a space, inline ones are not. A list item leaves out nested lists: their items are listed on their own.
  const blockText = (el) => {
    const parts = [];
    const walk = (node) => {
      for (const c of node.childNodes) {
        if (c.nodeType === Node.TEXT_NODE) { parts.push(c.textContent); continue; }
        if (c.nodeType !== Node.ELEMENT_NODE || NO_TEXT.has(c.tagName)) continue;
        if (el.tagName === 'LI' && (c.tagName === 'UL' || c.tagName === 'OL')) continue;
        const display = getComputedStyle(c).display;
        if (display === 'none') continue;
        const block = !display.startsWith('inline');
        if (block) parts.push(' ');
        walk(c);
        if (block) parts.push(' ');
      }
    };
    walk(el);
    return collapse(parts.join(''));
  };
  // The root is the page itself; a <body> that scrolls on its own (app-shell layouts) is a scroller like any other.
  const isScroller = (el, cs) =>
    el !== root &&
    ((/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 1) ||
     (/(auto|scroll)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 1));
  const clips = (cs) => cs.overflowX !== 'visible' || cs.overflowY !== 'visible';

  // What clip-path / clip leave of a w x h box: 'hidden' when nothing of it can paint (screen-reader
  // patterns such as inset(50%) or rect(0 0 0 0)), 'partial' for any other clip, 'none' when unclipped.
  // Lengths that are not plain px or % (calc(), keywords) parse to NaN and never count as hidden.
  const px = (v, ref) => (v.endsWith('%') ? (parseFloat(v) / 100) * ref : parseFloat(v));
  const clipState = (cs, w, h) => {
    let state = 'none';
    const m = /^(inset|circle|ellipse|polygon)\((.*)\)/.exec(cs.clipPath);
    if (cs.clipPath !== 'none') state = 'partial';
    if (m && m[1] === 'inset') {
      const [t, r = t, b = t, l = r] = m[2].split(' round ')[0].trim().split(/\s+/);
      if (h - px(t, h) - px(b, h) <= 0 || w - px(l, w) - px(r, w) <= 0) return 'hidden';
    } else if (m && m[1] !== 'polygon') {
      if (m[2].split('at ')[0].trim().split(/\s+/).some((v) => px(v, 1) === 0)) return 'hidden'; // zero radius
    } else if (m) {
      const pts = m[2].replace(/^(nonzero|evenodd),/, '').split(',')
        .map((p) => p.trim().split(/\s+/)).map(([x, y]) => [px(x, w), px(y, h)]);
      const area2 = pts.reduce((a, [x, y], i) => { // shoelace: twice the polygon's area
        const [x2, y2] = pts[(i + 1) % pts.length];
        return a + x * y2 - x2 * y;
      }, 0);
      if (Math.abs(area2) < 1) return 'hidden';
    }
    // clip applies to absolutely positioned boxes only; 'auto' edges are the box's own.
    if (/^(absolute|fixed)$/.test(cs.position) && cs.clip.startsWith('rect(')) {
      const [t, r, b, l] = cs.clip.slice(5, -1).split(/[\s,]+/)
        .map((v, i) => (v === 'auto' ? [0, w, h, 0][i] : parseFloat(v)));
      if (r <= l || b <= t) return 'hidden';
      if (t > 0 || l > 0 || r < w || b < h) state = 'partial';
    }
    return state;
  };

  // Does it paint an opaque surface: a solid background colour or media, at full opacity (its own and its
  // ancestors')? A transparent wrapper or a tinted overlay on top leaves what is below it visible.
  const MEDIA = new Set(['img', 'video', 'canvas', 'iframe', 'object', 'embed']);
  const opaque = (c) => {
    const bg = getComputedStyle(c).backgroundColor; // alpha < 1 serializes as rgba(...) or "... / a)"
    if (!MEDIA.has(c.tagName.toLowerCase()) && (bg.startsWith('rgba(') || bg.includes('/'))) return false;
    for (let a = c; a; a = a.parentElement) if (getComputedStyle(a).opacity !== '1') return false;
    return true;
  };

  // At one viewport point: is an opaque unrelated element (neither el, a descendant nor an ancestor) on top
  // of el (true), or is el or its own content reached first (false)? null when neither is found.
  const coveredAt = (el, x, y) => {
    for (const c of document.elementsFromPoint(x, y)) { // topmost first
      if (c === el || el.contains(c) || c.contains(el)) return false;
      if (opaque(c)) return true;
    }
    return null;
  };
  // Occluded: covered at its centre and all four corners (a box's content may sit anywhere in it).
  // Points outside the current viewport are unknown, so only a box wholly inside it can be occluded.
  // Hit testing skips pointer-events: none and hidden elements, so for those nothing found says anything.
  const occludedAt = (el, cs, vr) => {
    if (cs.pointerEvents === 'none' || cs.visibility !== 'visible') return null;
    const l = vr.x + 1, t = vr.y + 1, r = vr.x + vr.width - 1, b = vr.y + vr.height - 1;
    let all = true;
    for (const [x, y] of [[vr.x + vr.width / 2, vr.y + vr.height / 2], [l, t], [r, t], [l, b], [r, b]]) {
      const inside = x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight;
      const c = inside ? coveredAt(el, x, y) : null;
      if (c === false) return false;
      all = all && c === true;
    }
    return all ? true : null;
  };

  // Union of rects in viewport space; null when there is nothing to unite.
  const union = (rects) => {
    if (!rects.length) return null;
    const l = Math.min(...rects.map((r) => r.x)), t = Math.min(...rects.map((r) => r.y));
    const r = Math.max(...rects.map((r) => r.x + r.width)), b = Math.max(...rects.map((r) => r.y + r.height));
    return { x: l, y: t, width: r - l, height: b - t };
  };

  function visit(el, parentId, inherited) {
    const tag = el.tagName.toLowerCase();
    if (SKIP.has(tag)) return null;
    const cs = getComputedStyle(el);
    if (cs.display === 'none') return null; // not rendered, nor is anything inside it

    const node = { id: 'e' + out.length, parent_id: parentId, child_ids: [], tag };
    out.push(node);

    const contents = cs.display === 'contents';
    const own = contents ? null : rect(el.getBoundingClientRect());
    const pos = cs.position;
    const fixed = inherited.fixed || pos === 'fixed';
    const sticky = inherited.sticky || pos === 'sticky';
    const opacity = parseFloat(cs.opacity);
    const scroller = isScroller(el, cs);
    const clipped = own ? clipState(cs, own.width, own.height) : 'none';
    const childInherited = {
      fixed,
      sticky,
      transparent: inherited.transparent || opacity === 0,
      // Clipped to nothing by a zero-size ancestor, e.g. a collapsed accordion panel.
      collapsed: inherited.collapsed || (own !== null && clips(cs) && (own.width === 0 || own.height === 0)),
      inScroller: inherited.inScroller || scroller,
      clippedAway: inherited.clippedAway || clipped === 'hidden',
    };

    if (!LEAF.has(tag)) {
      for (const c of el.children) {
        const cid = visit(c, node.id, childInherited);
        if (cid) node.child_ids.push(cid);
      }
    }

    // display: contents has no box of its own: measure its painted content (visible children and
    // text), so an offscreen child such as a screen-reader-only span cannot stretch the wrapper.
    let vr = own;
    if (contents) {
      const parts = node.child_ids
        .map((id) => out[+id.slice(1)])
        .filter((c) => c.visibility.has_box && !c.visibility.offscreen)
        .map((c) => c.viewport_bounds);
      for (const n of el.childNodes) {
        if (n.nodeType !== Node.TEXT_NODE || !n.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(n);
        parts.push(rect(range.getBoundingClientRect()));
      }
      vr = union(parts) || { x: 0, y: 0, width: 0, height: 0 };
    }
    const bounds = { x: vr.x + scrollX, y: vr.y + scrollY, width: vr.width, height: vr.height };
    const hasBox = vr.width > 0 && vr.height > 0;
    // Content inside a scroller can sit outside the document area without being "offscreen";
    // it is uninspected, which the scroll_container limitation records.
    const offscreen = hasBox && !inherited.inScroller &&
      (bounds.x + bounds.width <= 0 || bounds.y + bounds.height <= 0 || bounds.x >= docW || bounds.y >= docH);
    const tiny = vr.width <= 1 && vr.height <= 1; // visually-hidden pattern (1x1 + clip)
    // Clipped-away boxes are skipped by hit testing too: nothing found over them says they are covered.
    const occluded = hasBox && clipped !== 'hidden' && !inherited.clippedAway ? occludedAt(el, cs, vr) : null;

    const limitations = [];
    if (tag === 'iframe') limitations.push('iframe');
    if (tag === 'object' || tag === 'embed') limitations.push('embed');
    if (tag === 'canvas') limitations.push('canvas');
    if (el.shadowRoot) limitations.push('shadow_root'); // open roots only; closed ones are undetectable
    if (scroller) limitations.push('scroll_container');

    Object.assign(node, {
      role: el.getAttribute('role'),
      html_id: el.id || null,
      alt: tag === 'img' ? el.getAttribute('alt') : null,
      path: pathOf(el),
      text: LEAF.has(tag) ? '' : ownText(el),
      // Links are read whole too: a button-like <a> often wraps its label in spans.
      full_text: LEAF.has(tag) ? '' : TEXT_BLOCKS.has(tag) || tag === 'a' ? blockText(el) : ownFullText(el),
      box: contents ? 'contents' : 'normal',
      bounds,
      viewport_bounds: vr,
      positioning: fixed ? 'fixed' : sticky ? 'sticky' : pos === 'absolute' ? 'absolute' : 'flow',
      styles: Object.fromEntries(STYLE_KEYS.map((k) => [k, cs[k]])),
      visibility: {
        css_visibility: cs.visibility,
        opacity,
        transparent_ancestor: inherited.transparent,
        collapsed_ancestor: inherited.collapsed,
        in_scroll_container: inherited.inScroller,
        has_box: hasBox,
        tiny,
        offscreen,
        clipped,
        clipped_ancestor: inherited.clippedAway,
        occluded,
        visible: cs.visibility === 'visible' && opacity > 0 && !inherited.transparent && !inherited.collapsed &&
                 hasBox && !tiny && !offscreen && clipped !== 'hidden' && !inherited.clippedAway && occluded !== true,
      },
      limitations,
    });
    return node.id;
  }

  visit(document.body, null,
    { fixed: false, sticky: false, transparent: false, collapsed: false, inScroller: false, clippedAway: false });
  return {
    scroll: { x: scrollX, y: scrollY },
    document: { x: 0, y: 0, width: docW, height: docH },
    elements: out,
  };
}
