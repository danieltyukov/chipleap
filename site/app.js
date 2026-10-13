// ChipLeap site: data-driven animations. Everything shown comes from data.json, which
// scripts/build_site_data.py generates from real ChipLeap runs.
(() => {
  const root = document.documentElement;
  root.classList.add('js');
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const NS = 'http://www.w3.org/2000/svg';
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  // Navigation border once the page scrolls.
  const nav = $('.nav');
  const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 8);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // Copy buttons.
  for (const b of $$('.copy')) {
    b.addEventListener('click', async () => {
      const text = b.parentElement.querySelector('code').textContent;
      try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; }
      catch { b.textContent = 'Select and copy'; }
      setTimeout(() => { b.textContent = 'Copy'; }, 1600);
    });
  }

  // Hero: callouts appear one by one, once; the render rises slightly as the page scrolls.
  const callouts = $$('.hero .callout');
  callouts.forEach((c, i) => setTimeout(() => c.classList.add('on'), reduce ? 0 : 700 + i * 380));
  const art = $('.art-frame');
  if (art && !reduce) {
    window.addEventListener('scroll', () => {
      const y = Math.min(window.scrollY, 600);
      art.style.setProperty('--lift', `${-y * 0.06}px`);
    }, { passive: true });
  }

  // Reveal-on-view helper.
  const onView = (el, fn, threshold = 0.35) => {
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) { fn(); io.disconnect(); }
    }, { threshold });
    io.observe(el);
  };

  onView($('#flow-line'), () => $('#flow-line').classList.add('run'));

  // The chip: its callouts appear once it is in view.
  onView($('.chip-frame'), () => {
    $$('.chip-frame .callout').forEach((c, i) => setTimeout(() => c.classList.add('on'), reduce ? 0 : 200 + i * 300));
  }, 0.3);

  // The digital flow: on phones only the first group starts open.
  if (window.matchMedia('(max-width: 640px)').matches) {
    $$('.rtl-flow details').forEach((d, i) => { if (i > 0) d.open = false; });
  }

  // Stats count up.
  onView($('.stats'), () => {
    for (const n of $$('.stat .num[data-to]')) {
      const to = Number(n.dataset.to), dec = Number(n.dataset.dec || 0), suffix = n.dataset.suffix || '';
      if (reduce) { n.firstChild.textContent = to.toFixed(dec) + suffix; continue; }
      const t0 = performance.now();
      const step = (t) => {
        const k = Math.min(1, (t - t0) / 1200);
        n.firstChild.textContent = (to * ease(k)).toFixed(dec) + suffix;
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
  });

  // Console: messages appear in order; the user's lines type themselves.
  onView($('#console'), () => {
    const lines = $$('#console .console-body p');
    let delay = 0;
    for (const p of lines) {
      const text = p.textContent;
      if (reduce) { p.classList.add('on'); continue; }
      if (p.classList.contains('c-user')) {
        p.textContent = '';
        setTimeout(() => {
          p.classList.add('on');
          let i = 0;
          const tick = () => { p.textContent = text.slice(0, ++i); if (i < text.length) setTimeout(tick, 16); };
          tick();
        }, delay);
        delay += text.length * 16 + 450;
      } else {
        setTimeout(() => p.classList.add('on'), delay);
        delay += p.classList.contains('c-tool') ? 500 : 900;
      }
    }
  }, 0.3);

  fetch('data.json').then((r) => r.json()).then((data) => {
    story(data.trace);
    calculator(data.calculator);
  }).catch(() => {});

  // The agent designs a chip: floorplan to scale at every accepted step.
  function story(trace) {
    const svg = $('#fp');
    const steps = $$('#story-steps li');
    if (!svg || !steps.length || !trace) return;
    const pts = trace.points;
    const maxSide = Math.max(...pts.map((p) => p.floorplan.side));
    const S = 330 / maxSide;
    const CX = 200, CY = 196;
    const colors = { sram: 'var(--fp-sram)', accel: 'var(--fp-accel)', buffer: 'var(--fp-buffer)',
      cpu: 'var(--fp-cpu)', soc: 'var(--fp-soc)', phy: 'var(--fp-phy)' };

    const el = (tag, attrs, parent) => {
      const e = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
      if (parent) parent.appendChild(e);
      return e;
    };
    const die = el('rect', { class: 'die', rx: 6 }, svg);
    const core = el('rect', { class: 'core', rx: 2 }, svg);
    const pads = el('g', {}, svg);
    const blocksG = el('g', {}, svg);
    const scaleLine = el('line', { class: 'scale-line' }, svg);
    const scaleText = el('text', { class: 'scale' }, svg);
    scaleText.textContent = '1 mm';
    const nodes = new Map();
    let geom = new Map();
    let dieGeom = null;
    let current = -1;
    let anim = 0;
    // Label size in viewBox units: 11 on a desktop panel, larger when the panel is drawn
    // small on a phone so the text stays readable at about 9 px.
    let fs = 11;
    const sizeLabels = () => { fs = Math.max(11, (9 * 400) / (svg.clientWidth || 400)); };
    sizeLabels();

    const target = (p) => {
      const side = p.floorplan.side * S;
      const ox = CX - side / 2, oy = CY - side / 2;
      const g = new Map();
      for (const b of p.floorplan.blocks) {
        g.set(b.id, { x: ox + b.x * S, y: oy + (p.floorplan.side - b.y - b.h) * S, w: b.w * S, h: b.h * S,
          kind: b.kind, label: b.label, short: b.short });
      }
      return { side, ox, oy, blocks: g };
    };

    const drawPads = (d) => {
      pads.replaceChildren();
      const ring = d.side * 0.06;
      const n = Math.max(6, Math.round(d.side / 14));
      const pitch = (d.side - 2 * ring) / n;
      const s = Math.min(ring * 0.55, pitch * 0.6);
      for (let i = 0; i < n; i++) {
        const u = d.ox + ring + pitch * (i + 0.5) - s / 2;
        const v = d.oy + ring + pitch * (i + 0.5) - s / 2;
        el('rect', { class: 'pad', x: u, y: d.oy + ring / 2 - s / 2, width: s, height: s }, pads);
        el('rect', { class: 'pad', x: u, y: d.oy + d.side - ring / 2 - s / 2, width: s, height: s }, pads);
        el('rect', { class: 'pad', x: d.ox + ring / 2 - s / 2, y: v, width: s, height: s }, pads);
        el('rect', { class: 'pad', x: d.ox + d.side - ring / 2 - s / 2, y: v, width: s, height: s }, pads);
      }
    };

    const paint = (d, blocks, alpha) => {
      die.setAttribute('x', d.ox); die.setAttribute('y', d.oy);
      die.setAttribute('width', d.side); die.setAttribute('height', d.side);
      const ring = d.side * 0.06;
      core.setAttribute('x', d.ox + ring); core.setAttribute('y', d.oy + ring);
      core.setAttribute('width', d.side - 2 * ring); core.setAttribute('height', d.side - 2 * ring);
      for (const [id, b] of blocks) {
        let n = nodes.get(id);
        if (!n) {
          const g = el('g', {}, blocksG);
          n = { g, rect: el('rect', { class: 'blk', rx: 1.5 }, g), text: el('text', { class: 'lbl' }, g) };
          nodes.set(id, n);
        }
        n.rect.setAttribute('x', b.x); n.rect.setAttribute('y', b.y);
        n.rect.setAttribute('width', Math.max(0, b.w)); n.rect.setAttribute('height', Math.max(0, b.h));
        n.rect.setAttribute('fill', colors[b.kind]);
        n.g.setAttribute('opacity', alpha.get(id) ?? 1);
        // The full label if it fits, else the short one, else nothing.
        const fits = (t) => t && t.length * fs * 0.6 + fs + 1 < b.w && b.h > fs * 2;
        n.text.textContent = fits(b.label) ? b.label : fits(b.short) ? b.short : '';
        n.text.style.fontSize = `${fs}px`;
        n.text.setAttribute('x', b.x + fs * 0.55); n.text.setAttribute('y', b.y + fs * 1.45);
      }
      for (const [id, n] of nodes) if (!blocks.has(id)) { n.g.remove(); nodes.delete(id); }
      scaleLine.setAttribute('x1', 22); scaleLine.setAttribute('x2', 22 + S);
      scaleLine.setAttribute('y1', 388); scaleLine.setAttribute('y2', 388);
      scaleText.setAttribute('x', 28 + S); scaleText.setAttribute('y', 392);
      scaleText.style.fontSize = `${Math.max(12, fs)}px`;
    };

    const fmtTps = (v) => (v >= 10 ? v.toFixed(1) : v >= 1 ? v.toFixed(2) : v.toPrecision(2));
    const spark = $('#spark');
    const sparkInit = () => {
      const W = 240, H = 64, pad = 6;
      const lo = Math.min(...pts.map((p) => p.tps)) / 1.5, hi = Math.max(...pts.map((p) => p.tps), trace.target) * 1.5;
      const x = (i) => pad + i * (W - 2 * pad) / Math.max(1, pts.length - 1);
      const y = (v) => H - pad - (Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo)) * (H - 2 * pad);
      const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.tps).toFixed(1)}`).join(' ');
      el('line', { class: 'target', x1: pad, x2: W - pad, y1: y(trace.target), y2: y(trace.target) }, spark);
      el('path', { class: 'path', d }, spark);
      const on = el('path', { class: 'path-on', d }, spark);
      const len = on.getTotalLength();
      on.style.strokeDasharray = `${len}`;
      const dots = pts.map((p, i) => el('circle', { class: 'pt', cx: x(i), cy: y(p.tps), r: 3.5 }, spark));
      const partial = pts.map((_, i) => {
        if (i === 0) return 0;
        const sub = document.createElementNS(NS, 'path');
        sub.setAttribute('d', pts.slice(0, i + 1).map((p, j) => `${j ? 'L' : 'M'}${x(j)},${y(p.tps)}`).join(' '));
        return sub.getTotalLength();
      });
      return (i) => {
        on.style.strokeDashoffset = `${len - partial[i]}`;
        dots.forEach((dot, j) => dot.classList.toggle('done', j <= i));
      };
    };
    const setSpark = sparkInit();

    const show = (i) => {
      if (i === current) return;
      const p = pts[i];
      const t = target(p);
      $('#p-node').textContent = p.node_label;
      $('#p-mem').textContent = p.mem_label;
      $('#p-area').textContent = `${p.mm2.toFixed(2)} mm\u00b2`;
      setSpark(i);
      steps.forEach((s, j) => s.classList.toggle('active', j === Math.min(i, steps.length - 1)));
      const fromT = Number($('#p-tps').dataset.v || p.tps);
      $('#p-tps').dataset.v = p.tps;
      cancelAnimationFrame(anim);
      if (reduce || current === -1) {
        drawPads(t); paint(t, t.blocks, new Map());
        geom = t.blocks; dieGeom = t; current = i;
        $('#p-tps').textContent = fmtTps(p.tps);
        return;
      }
      const from = geom, fromDie = dieGeom;
      const t0 = performance.now(), D = 750;
      current = i;
      const frame = (now) => {
        const k = ease(Math.min(1, (now - t0) / D));
        const lerp = (a, b) => a + (b - a) * k;
        const d = { side: lerp(fromDie.side, t.side), ox: lerp(fromDie.ox, t.ox), oy: lerp(fromDie.oy, t.oy) };
        const blocks = new Map(), alpha = new Map();
        for (const [id, b] of t.blocks) {
          const a = from.get(id);
          if (a) blocks.set(id, { ...b, x: lerp(a.x, b.x), y: lerp(a.y, b.y), w: lerp(a.w, b.w), h: lerp(a.h, b.h) });
          else { blocks.set(id, b); alpha.set(id, k); }
        }
        for (const [id, a] of from) if (!t.blocks.has(id) && k < 1) { blocks.set(id, a); alpha.set(id, 1 - k); }
        paint(d, blocks, alpha);
        const lv = Math.exp(Math.log(fromT) + (Math.log(p.tps) - Math.log(fromT)) * k);
        $('#p-tps').textContent = fmtTps(lv);
        if (k < 1) anim = requestAnimationFrame(frame);
        else { drawPads(t); geom = t.blocks; dieGeom = t; }
      };
      drawPads(t);
      anim = requestAnimationFrame(frame);
    };

    show(0);
    window.addEventListener('resize', () => {
      const before = fs;
      sizeLabels();
      if (fs !== before && dieGeom) paint(dieGeom, geom, new Map());
    });
    // A step becomes current as it crosses the middle of the screen; on narrow screens the
    // floorplan sits pinned above the steps, so the band moves down below it.
    const narrow = window.matchMedia('(max-width: 980px)').matches;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) show(Number(e.target.dataset.i));
    }, { rootMargin: narrow ? '-52% 0px -38% 0px' : '-45% 0px -45% 0px' });
    steps.forEach((s) => io.observe(s));
  }

  function calculator(data) {
    if (!data || !$('#c-model')) return;
    for (const m of data.models) $('#c-model').add(new Option(m.name, m.key, false, m.key === 'llama-3.2-1b'));
    for (const m of data.memories) $('#c-mem').add(new Option(m.name, m.key, false, m.key === 'lpddr4-x16'));
    const fmt = (x) => (x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x >= 1 ? x.toFixed(2) : x.toPrecision(2));
    const update = () => {
      const model = data.models.find((m) => m.key === $('#c-model').value);
      const mem = data.memories.find((m) => m.key === $('#c-mem').value);
      const w = $('#c-fmt').value, ch = Number($('#c-ch').value);
      const bytes = model.bytes[w], need = model.capacity[w];
      $('#c-out').textContent = fmt((mem.sustained * ch) / bytes);
      const notes = [`${(bytes / 1e6).toFixed(0)} MB read per token`];
      let bad = false;
      if (mem.capacity * ch < need) { bad = true; notes.push(`${(need / 2 ** 20).toFixed(0)} MiB of weights and KV cache do not fit in ${ch} x ${(mem.capacity / 2 ** 20).toFixed(0)} MiB`); }
      if (mem.read_only) { bad = true; notes.push('flash is read only, so the KV cache needs another home'); }
      const nodes = mem.nodes.map((n) => data.techs[n].replace(/ \(open PDK\)/, ''));
      notes.push(nodes.length ? `buildable at ${nodes.join(', ')}` : 'no listed node can host this interface');
      $('#c-verdict').textContent = notes.join('; ') + '.';
      $('#c-verdict').classList.toggle('bad', bad);
    };
    for (const id of ['#c-model', '#c-fmt', '#c-mem', '#c-ch']) $(id).addEventListener('change', update);
    update();
  }
})();
