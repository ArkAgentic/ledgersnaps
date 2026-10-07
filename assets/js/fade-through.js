(function (global) {
  const FADE_DURATION_S = 0.55;
  const FADE_EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';

  function reducedMotion() {
    return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function apply(el, cfg) {
    if (!el) return;
    Object.assign(el.style, cfg);
  }

  async function swap({ outEl, inEl, hideClass = 'hidden', onBeforeIn } = {}) {
    if (!outEl || !inEl) return;

    if (reducedMotion()) {
      if (onBeforeIn) onBeforeIn();
      outEl.classList.add(hideClass);
      inEl.classList.remove(hideClass);
      return;
    }

    const parent = outEl.parentElement;
    const prevMinHeight = parent ? parent.style.minHeight : '';

    outEl.classList.remove(hideClass);
    inEl.classList.remove(hideClass);

    // Lock height during cross-fade to prevent vertical jump
    if (parent) {
      const h = Math.max(outEl.offsetHeight || 0, inEl.offsetHeight || 0);
      if (h > 0) parent.style.minHeight = `${h}px`;
    }

    if (onBeforeIn) onBeforeIn();

    // Pure fade only: no translate/scale/blur movement
    apply(inEl, {
      opacity: '0',
      transition: `opacity ${FADE_DURATION_S}s ${FADE_EASE}`,
    });
    apply(outEl, {
      opacity: '1',
      transition: `opacity ${FADE_DURATION_S}s ${FADE_EASE}`,
    });

    // force style flush
    void inEl.offsetHeight;

    apply(inEl, { opacity: '1' });
    apply(outEl, { opacity: '0' });

    await new Promise((r) => setTimeout(r, FADE_DURATION_S * 1000));

    outEl.classList.add(hideClass);

    apply(inEl, { transition: '', opacity: '' });
    apply(outEl, { transition: '', opacity: '' });

    if (parent) parent.style.minHeight = prevMinHeight || '';
  }

  global.FadeThrough = { swap };
})(window);
