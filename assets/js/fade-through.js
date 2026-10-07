(function (global) {
  const DURATION_MS = 320;
  const EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';
  const activeByParent = new WeakMap();

  function reducedMotion() {
    return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function stopActive(parent) {
    const running = parent && activeByParent.get(parent);
    if (!running) return;
    try { running.cancelled = true; running.outAnim?.cancel(); running.inAnim?.cancel(); } catch (_) {}
    activeByParent.delete(parent);
  }

  function waitFinished(anim) {
    return anim?.finished?.catch(() => undefined) || Promise.resolve();
  }

  async function swap({ outEl, inEl, hideClass = 'hidden', onBeforeIn } = {}) {
    if (!outEl || !inEl || outEl === inEl) return;

    const parent = outEl.parentElement || inEl.parentElement;
    if (parent) stopActive(parent);

    if (reducedMotion()) {
      if (onBeforeIn) onBeforeIn();
      outEl.classList.add(hideClass);
      inEl.classList.remove(hideClass);
      return;
    }

    const ctx = { cancelled: false, outAnim: null, inAnim: null };
    if (parent) activeByParent.set(parent, ctx);

    // Freeze container height during transition to avoid any reflow jump.
    const prevMinHeight = parent ? parent.style.minHeight : '';
    if (parent) {
      outEl.classList.remove(hideClass);
      inEl.classList.remove(hideClass);
      const lockH = Math.max(outEl.offsetHeight || 0, inEl.offsetHeight || 0);
      if (lockH > 0) parent.style.minHeight = `${lockH}px`;
      if (inEl.classList.contains(hideClass)) inEl.classList.add(hideClass);
    }

    // Fade out current pane only (no translate/scale/blur)
    outEl.style.willChange = 'opacity';
    ctx.outAnim = outEl.animate([{ opacity: 1 }, { opacity: 0 }], {
      duration: DURATION_MS,
      easing: EASE,
      fill: 'forwards',
    });
    await waitFinished(ctx.outAnim);
    if (ctx.cancelled) return;

    if (onBeforeIn) onBeforeIn();

    outEl.classList.add(hideClass);
    inEl.classList.remove(hideClass);

    // Fade in target pane
    inEl.style.opacity = '0';
    inEl.style.willChange = 'opacity';
    ctx.inAnim = inEl.animate([{ opacity: 0 }, { opacity: 1 }], {
      duration: DURATION_MS,
      easing: EASE,
      fill: 'forwards',
    });
    await waitFinished(ctx.inAnim);
    if (ctx.cancelled) return;

    outEl.style.opacity = '';
    inEl.style.opacity = '';
    outEl.style.willChange = '';
    inEl.style.willChange = '';

    if (parent) parent.style.minHeight = prevMinHeight || '';
    if (parent) activeByParent.delete(parent);
  }

  global.FadeThrough = { swap };
})(window);
