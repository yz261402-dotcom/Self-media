(() => {
  'use strict';

  function initializeReader() {
    const article = document.getElementById('document-body');
    if (!article) return;
    const root = document.documentElement;
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const scrollBehavior = () => reducedMotion.matches ? 'instant' : 'smooth';

    // Local files and privacy settings can deny storage; controls remain usable.
    const fontKey = 'document-reader-font-size';
    let fontSize = 17;
    try {
      const savedSize = Number(localStorage.getItem(fontKey));
      if (Number.isFinite(savedSize) && savedSize >= 14 && savedSize <= 22) fontSize = savedSize;
    } catch (_) { /* Storage is optional. */ }
    const smaller = document.querySelector('.font-smaller');
    const reset = document.querySelector('.font-reset');
    const larger = document.querySelector('.font-larger');
    function setFontSize(value) {
      fontSize = Math.max(14, Math.min(22, value));
      root.style.setProperty('--reader-font-size', `${fontSize}px`);
      if (smaller) smaller.disabled = fontSize === 14;
      if (larger) larger.disabled = fontSize === 22;
      if (reset) reset.title = `恢复默认字号（当前 ${fontSize}px）`;
      try { localStorage.setItem(fontKey, String(fontSize)); } catch (_) { /* Optional. */ }
    }
    setFontSize(fontSize);
    smaller?.addEventListener('click', () => setFontSize(fontSize - 1));
    reset?.addEventListener('click', () => setFontSize(17));
    larger?.addEventListener('click', () => setFontSize(fontSize + 1));
    document.querySelector('.print-button')?.addEventListener('click', () => window.print());

    const menu = document.querySelector('.mobile-menu');
    const sidebar = document.getElementById('doc-sidebar');
    const scrim = document.querySelector('.sidebar-scrim');
    const mobileWidth = window.matchMedia('(max-width: 800px)');
    function setMenu(open, returnFocus = false) {
      const visible = open && mobileWidth.matches;
      document.body.classList.toggle('menu-open', visible);
      menu?.setAttribute('aria-expanded', String(visible));
      if (scrim) scrim.hidden = !visible;
      if (visible) {
        sidebar?.querySelector('[aria-current="page"], a, button')?.focus();
      } else if (returnFocus) menu?.focus();
    }
    menu?.addEventListener('click', () => setMenu(!document.body.classList.contains('menu-open')));
    scrim?.addEventListener('click', () => setMenu(false, true));
    sidebar?.addEventListener('click', (event) => {
      if (event.target.closest('a')) setMenu(false);
    });
    mobileWidth.addEventListener('change', () => setMenu(false));
    document.addEventListener('keydown', (event) => {
      if (!document.body.classList.contains('menu-open')) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        setMenu(false, true);
      }
      if (event.key === 'Tab') {
        const focusable = [menu, ...sidebar.querySelectorAll('a[href], button:not([disabled]), [tabindex="0"]')].filter(Boolean);
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    });
    setMenu(false);

    const searchForm = document.getElementById('search-form');
    const searchInput = document.getElementById('search-input');
    const searchNext = document.getElementById('search-next');
    const searchClear = document.getElementById('search-clear');
    const searchStatus = document.getElementById('search-status');
    let searchResults = [];
    let currentResult = -1;
    let currentQuery = '';
    let searchTimer;

    function clearHighlights() {
      article.querySelectorAll('mark.reader-match').forEach((mark) => {
        const parent = mark.parentNode;
        mark.replaceWith(document.createTextNode(mark.textContent));
        parent.normalize();
      });
      article.querySelectorAll('.search-block-current').forEach((block) => block.classList.remove('search-block-current'));
      searchResults = [];
      currentResult = -1;
    }

    function showResult(index, shouldScroll) {
      searchResults.forEach((result) => {
        result.block.classList.remove('search-block-current');
        result.marks.forEach((mark) => mark.classList.remove('is-current'));
      });
      currentResult = (index + searchResults.length) % searchResults.length;
      const result = searchResults[currentResult];
      result.block.classList.add('search-block-current');
      result.marks.forEach((mark) => mark.classList.add('is-current'));
      if (searchStatus) searchStatus.textContent = `第 ${currentResult + 1} / ${searchResults.length} 处匹配 · Enter 下一处`;
      if (shouldScroll) {
        let container = result.block.parentElement;
        while (container && container !== article) {
          if (container.tagName === 'DETAILS') container.open = true;
          container = container.parentElement;
        }
        result.marks[0].scrollIntoView({ behavior: scrollBehavior(), block: 'center', inline: 'nearest' });
      }
    }

    function runSearch(shouldScroll = false) {
      clearTimeout(searchTimer);
      clearHighlights();
      const query = searchInput?.value.trim() || '';
      currentQuery = query;
      if (searchClear) searchClear.disabled = !query;
      if (!query) {
        if (searchStatus) searchStatus.textContent = '';
        if (searchNext) searchNext.disabled = true;
        return;
      }
      const queryLower = query.toLocaleLowerCase();
      const blockResults = new Map();
      // Walk original text nodes, preserving links, emphasis, tables and code.
      const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          if (!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
          if (node.parentElement.closest('script, style, button, svg, mark, [aria-hidden="true"]')) return NodeFilter.FILTER_REJECT;
          return node.nodeValue.toLocaleLowerCase().includes(queryLower) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        }
      });
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach((node) => {
        const original = node.nodeValue;
        const normalized = original.toLocaleLowerCase();
        const block = node.parentElement.closest('p, li, td, th, h2, h3, h4, h5, h6, pre, summary, figcaption') || node.parentElement;
        let result = blockResults.get(block);
        if (!result) {
          result = { block, marks: [] };
          blockResults.set(block, result);
        }
        const fragment = document.createDocumentFragment();
        let position = 0;
        let matchPosition;
        while ((matchPosition = normalized.indexOf(queryLower, position)) !== -1) {
          fragment.append(document.createTextNode(original.slice(position, matchPosition)));
          const mark = document.createElement('mark');
          mark.className = 'reader-match';
          mark.textContent = original.slice(matchPosition, matchPosition + query.length);
          fragment.append(mark);
          result.marks.push(mark);
          position = matchPosition + query.length;
        }
        fragment.append(document.createTextNode(original.slice(position)));
        node.replaceWith(fragment);
      });
      searchResults = [...blockResults.values()];
      if (searchNext) searchNext.disabled = !searchResults.length;
      if (!searchResults.length) {
        if (searchStatus) searchStatus.textContent = '未找到匹配内容';
      } else showResult(0, shouldScroll);
    }

    searchInput?.addEventListener('input', (event) => {
      if (event.isComposing) return;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => runSearch(false), 180);
    });
    searchInput?.addEventListener('compositionend', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => runSearch(false), 180);
    });
    searchForm?.addEventListener('submit', (event) => {
      event.preventDefault();
      const queryChanged = currentQuery !== searchInput.value.trim();
      if (queryChanged) runSearch(true);
      else if (searchResults.length) showResult(currentResult + 1, true);
    });
    if (searchNext && searchNext.type !== 'submit') {
      searchNext.addEventListener('click', () => {
        if (currentQuery !== searchInput.value.trim()) runSearch(true);
        else if (searchResults.length) showResult(currentResult + 1, true);
      });
    }
    function resetSearch() {
      clearTimeout(searchTimer);
      if (searchInput) searchInput.value = '';
      currentQuery = '';
      clearHighlights();
      if (searchStatus) searchStatus.textContent = '';
      if (searchNext) searchNext.disabled = true;
      if (searchClear) searchClear.disabled = true;
    }
    searchClear?.addEventListener('click', (event) => {
      event.preventDefault();
      resetSearch();
      searchInput?.focus();
    });
    searchInput?.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !event.isComposing) {
        event.preventDefault();
        resetSearch();
      }
      if (event.key === 'Enter' && event.isComposing) event.preventDefault();
    });
    resetSearch();

    async function copyText(text) {
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch (_) { /* File URLs may reject the modern Clipboard API. */ }
      const previousFocus = document.activeElement;
      const selection = window.getSelection();
      const savedRanges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
      const field = document.createElement('textarea');
      field.value = text;
      field.readOnly = true;
      field.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;font-size:16px';
      document.body.append(field);
      field.select();
      let copied = false;
      try { copied = document.execCommand('copy'); } catch (_) { /* Show the failure state. */ }
      field.remove();
      if (selection) {
        selection.removeAllRanges();
        savedRanges.forEach((range) => selection.addRange(range));
      }
      previousFocus?.focus({ preventScroll: true });
      return copied;
    }

    article.querySelectorAll('pre').forEach((pre) => {
      const code = pre.querySelector('code');
      if (!code || pre.querySelector('.copy-code')) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy-code';
      button.textContent = '复制代码';
      button.setAttribute('aria-label', '复制代码');
      button.setAttribute('aria-live', 'polite');
      pre.append(button);
      let labelTimer;
      button.addEventListener('click', async () => {
        clearTimeout(labelTimer);
        const copied = await copyText(code.textContent);
        button.textContent = copied ? '已复制' : '请选中后复制';
        labelTimer = setTimeout(() => { button.textContent = '复制代码'; }, 2200);
      });
    });

    // Scroll-spy works without observers and avoids changing browser history.
    const tocLinks = Array.from(document.querySelectorAll('#toc a[href^="#"]'));
    const sections = tocLinks.map((link) => {
      let id;
      try { id = decodeURIComponent(link.getAttribute('href').slice(1)); } catch (_) { return null; }
      const heading = document.getElementById(id);
      return heading ? { link, heading } : null;
    }).filter(Boolean);
    let activeLink = null;
    const backTop = document.querySelector('.back-top');
    let framePending = false;
    function updateScrollState() {
      framePending = false;
      if (backTop) backTop.hidden = window.scrollY < 480;
      let active = sections[0];
      const header = document.querySelector('.topbar');
      const threshold = (header?.getBoundingClientRect().height || 76) + 38;
      for (const section of sections) {
        if (section.heading.getBoundingClientRect().top <= threshold) active = section;
        else break;
      }
      if (!active || active.link === activeLink) return;
      activeLink?.removeAttribute('aria-current');
      activeLink?.classList.remove('is-active');
      activeLink = active.link;
      activeLink.setAttribute('aria-current', 'location');
      activeLink.classList.add('is-active');
      const panel = document.querySelector('.toc-panel');
      if (panel && panel.clientHeight) {
        const linkRect = activeLink.getBoundingClientRect();
        const panelRect = panel.getBoundingClientRect();
        if (linkRect.top < panelRect.top + 25 || linkRect.bottom > panelRect.bottom - 25) {
          panel.scrollTop += linkRect.top - panelRect.top - panel.clientHeight / 3;
        }
      }
    }
    function queueScrollUpdate() {
      if (!framePending) {
        framePending = true;
        requestAnimationFrame(updateScrollState);
      }
    }
    window.addEventListener('scroll', queueScrollUpdate, { passive: true });
    window.addEventListener('resize', queueScrollUpdate, { passive: true });
    backTop?.addEventListener('click', () => window.scrollTo({ top: 0, behavior: scrollBehavior() }));
    updateScrollState();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initializeReader, { once: true });
  else initializeReader();
})();
