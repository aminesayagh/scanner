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
  const ownText = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === Node.TEXT_NODE) t += n.textContent;
    t = t.replace(/\s+/g, ' ').trim();
    return t.length > 80 ? t.slice(0, 79) + '…' : t;
  };
  // The root is the page itself; a <body> that scrolls on its own (app-shell layouts) is a scroller like any other.
  const isScroller = (el, cs) =>
    el !== root &&
    ((/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 1) ||
     (/(auto|scroll)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 1));
  const clips = (cs) => cs.overflowX !== 'visible' || cs.overflowY !== 'visible';

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
    const childInherited = {
      fixed,
      sticky,
      transparent: inherited.transparent || opacity === 0,
      // Clipped to nothing by a zero-size ancestor, e.g. a collapsed accordion panel.
      collapsed: inherited.collapsed || (own !== null && clips(cs) && (own.width === 0 || own.height === 0)),
      inScroller: inherited.inScroller || scroller,
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

    const limitations = [];
    if (tag === 'iframe') limitations.push('iframe');
    if (tag === 'object' || tag === 'embed') limitations.push('embed');
    if (tag === 'canvas') limitations.push('canvas');
    if (el.shadowRoot) limitations.push('shadow_root'); // open roots only; closed ones are undetectable
    if (scroller) limitations.push('scroll_container');

    Object.assign(node, {
      role: el.getAttribute('role'),
      text: LEAF.has(tag) ? '' : ownText(el),
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
        visible: cs.visibility === 'visible' && opacity > 0 && !inherited.transparent && !inherited.collapsed &&
                 hasBox && !tiny && !offscreen,
      },
      limitations,
    });
    return node.id;
  }

  visit(document.body, null, { fixed: false, sticky: false, transparent: false, collapsed: false, inScroller: false });
  return {
    scroll: { x: scrollX, y: scrollY },
    document: { x: 0, y: 0, width: docW, height: docH },
    elements: out,
  };
}
