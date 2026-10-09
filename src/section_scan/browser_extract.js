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
  const isScroller = (el, cs) =>
    el !== root && el !== document.body &&
    ((/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 1) ||
     (/(auto|scroll)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 1));

  function visit(el, parentId, inherited) {
    const tag = el.tagName.toLowerCase();
    if (SKIP.has(tag)) return null;
    const cs = getComputedStyle(el);
    if (cs.display === 'none') return null; // not rendered, nor is anything inside it

    const node = { id: 'e' + out.length, parent_id: parentId, child_ids: [], tag };
    out.push(node);

    const pos = cs.position;
    const fixed = inherited.fixed || pos === 'fixed';
    const sticky = inherited.sticky || pos === 'sticky';
    const opacity = parseFloat(cs.opacity);
    const childInherited = { fixed, sticky, transparent: inherited.transparent || opacity === 0 };

    if (!LEAF.has(tag)) {
      for (const c of el.children) {
        const cid = visit(c, node.id, childInherited);
        if (cid) node.child_ids.push(cid);
      }
    }

    // display: contents has no box of its own; measure what it contains instead.
    const contents = cs.display === 'contents';
    let vr;
    if (contents) {
      const range = document.createRange();
      range.selectNodeContents(el);
      vr = rect(range.getBoundingClientRect());
    } else {
      vr = rect(el.getBoundingClientRect());
    }
    const bounds = { x: vr.x + scrollX, y: vr.y + scrollY, width: vr.width, height: vr.height };
    const hasBox = vr.width > 0 && vr.height > 0;
    const offscreen = hasBox && (bounds.x + bounds.width <= 0 || bounds.y + bounds.height <= 0 ||
                                 bounds.x >= docW || bounds.y >= docH);

    const limitations = [];
    if (tag === 'iframe') limitations.push('iframe');
    if (tag === 'canvas') limitations.push('canvas');
    if (el.shadowRoot) limitations.push('shadow_root'); // open roots only; closed ones are undetectable
    if (isScroller(el, cs)) limitations.push('scroll_container');

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
        has_box: hasBox,
        offscreen,
        visible: cs.visibility === 'visible' && opacity > 0 && !inherited.transparent && hasBox && !offscreen,
      },
      limitations,
    });
    return node.id;
  }

  visit(document.body, null, { fixed: false, sticky: false, transparent: false });
  return {
    scroll: { x: scrollX, y: scrollY },
    document: { x: 0, y: 0, width: docW, height: docH },
    elements: out,
  };
}
