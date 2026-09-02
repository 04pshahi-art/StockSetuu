/* Pratyush Computer Services — front-end.
 *
 * Two jobs, no framework:
 *   1. Barcode scanning. A USB scanner is a keyboard: it types the code and presses
 *      Enter. So the scan field just has to stay focused and treat Enter as "look this up".
 *   2. The line-item editor on the sale / purchase / service forms, including a live GST
 *      preview that uses the same integer-paise arithmetic as app/gst.py, so the number
 *      on screen matches the number that gets saved.
 */
'use strict';

/* -- integer money ------------------------------------------------------- */
/* Everything is paise. Floats never touch a tax figure. */

function toPaise(text) {
  const raw = String(text == null ? '' : text).replace(/[₹,\s]/g, '').trim();
  if (raw === '' || raw === '-' || raw === '.') return 0;
  const match = /^(-?)(\d*)(?:\.(\d*))?$/.exec(raw);
  if (!match) return 0;
  const sign = match[1] === '-' ? -1 : 1;
  const whole = match[2] || '0';
  let frac = (match[3] || '').slice(0, 3);
  while (frac.length < 3) frac += '0';
  // Third decimal digit decides the half-up rounding of the paise.
  const base = parseInt(whole, 10) * 100 + parseInt(frac.slice(0, 2), 10);
  const carry = parseInt(frac[2], 10) >= 5 ? 1 : 0;
  return sign * (base + carry);
}

/** Half-away-from-zero, matching money.mul_div_round in Python. */
function mulDivRound(amount, numerator, denominator) {
  if (!denominator) return 0;
  const product = amount * numerator;
  const negative = product < 0;
  const abs = Math.abs(product);
  const result = Math.floor((abs * 2 + denominator) / (denominator * 2));
  return negative ? -result : result;
}

function fmtPaise(paise) {
  const negative = paise < 0;
  const abs = Math.abs(Math.round(paise));
  const rupees = Math.floor(abs / 100);
  const cents = String(abs % 100).padStart(2, '0');
  // Indian grouping: last three digits, then pairs.
  const digits = String(rupees);
  let grouped;
  if (digits.length <= 3) {
    grouped = digits;
  } else {
    const tail = digits.slice(-3);
    let head = digits.slice(0, -3);
    const parts = [];
    while (head.length > 2) {
      parts.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) parts.unshift(head);
    grouped = parts.join(',') + ',' + tail;
  }
  return (negative ? '-' : '') + grouped + '.' + cents;
}

function rateLabel(bp) {
  const value = Number(bp) || 0;
  return (value % 100 === 0 ? String(value / 100) : (value / 100).toFixed(2)) + '%';
}

/* -- tiny helpers -------------------------------------------------------- */

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (value == null || value === false) return;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value);
  });
  (children || []).forEach((child) => node.appendChild(child));
  return node;
}

function toast(message, kind) {
  const host = document.querySelector('.content');
  if (!host) return;
  const box = el('div', { class: 'flash ' + (kind || 'ok') + ' noprint', text: message });
  host.insertBefore(box, host.firstChild);
  setTimeout(() => box.remove(), 4200);
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error('Request failed (' + response.status + ')');
  return response.json();
}

/* -- sidebar ------------------------------------------------------------- */

function initChrome() {
  const toggle = document.getElementById('menuToggle');
  const sidebar = document.getElementById('sidebar');
  if (!toggle || !sidebar) return;

  // One place to change the state, so aria-expanded can never drift out of step with the
  // drawer — a screen reader still announcing "expanded" over a closed menu is its own bug.
  function setOpen(open) {
    sidebar.classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  toggle.addEventListener('click', () => setOpen(!sidebar.classList.contains('open')));
  sidebar.addEventListener('click', (event) => {
    if (event.target.closest('a')) setOpen(false);
  });
  // On a phone the drawer covers most of the screen. Tapping the page or pressing Escape
  // has to close it; hunting for the ☰ again is not something a counter does mid-sale.
  document.addEventListener('click', (event) => {
    if (!sidebar.classList.contains('open')) return;
    if (sidebar.contains(event.target) || toggle.contains(event.target)) return;
    setOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && sidebar.classList.contains('open')) {
      setOpen(false);
      toggle.focus();
    }
  });
}

/* On phones the invoice needs an obvious way to reach the print dialogue. */
function initPrintButtons() {
  document.querySelectorAll('[data-print]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      window.print();
    });
  });
}

/* Stack-mode tables need each cell to know its column heading. */
function initStackLabels() {
  document.querySelectorAll('table.stack').forEach((table) => {
    const headings = Array.from(table.querySelectorAll('thead th')).map((th) =>
      th.textContent.trim()
    );
    table.querySelectorAll('tbody tr').forEach((row) => {
      Array.from(row.children).forEach((cell, index) => {
        if (!cell.hasAttribute('data-label') && headings[index]) {
          cell.setAttribute('data-label', headings[index]);
        }
      });
    });
  });
}

/* -- line editor --------------------------------------------------------- */

const GST_SLABS = [0, 250, 500, 1200, 1800, 2800];

class LineEditor {
  constructor(form) {
    this.form = form;
    this.mode = form.dataset.lineEditor; // 'sale' | 'purchase' | 'service'
    this.priceField = form.dataset.priceField || 'unit_price';
    this.shopState = form.dataset.shopState || '27';
    this.body = form.querySelector('[data-lines-body]');
    this.hidden = form.querySelector('[data-lines-json]');
    this.totalsBox = form.querySelector('[data-totals]');
    this.scanInput = form.querySelector('[data-scan]');
    this.searchInput = form.querySelector('[data-search]');
    this.searchResults = form.querySelector('[data-search-results]');
    this.lines = [];
    // Purchases do show a GST split, but only as our own reading of the lines — the figures
    // that get saved are the ones typed from the dealer's printed bill.
    this.taxEnabled = this.mode !== 'service';

    this.bind();
    this.render();
    this.focusScanner();
  }

  /* -- state -- */

  interstate() {
    if (this.mode === 'purchase') {
      const select = this.form.querySelector('[name="dealer_id"]');
      const states = JSON.parse(this.form.dataset.dealerStates || '{}');
      const state = select ? states[select.value] : '';
      return Boolean(state) && String(state) !== String(this.shopState);
    }
    const stateSelect = this.form.querySelector('[name="customer_state_code"]');
    const state = stateSelect ? stateSelect.value.trim() : '';
    // A blank state means a walk-in customer, i.e. our own state.
    return Boolean(state) && state !== String(this.shopState);
  }

  pricesIncludeGst() {
    const box = this.form.querySelector('[name="prices_include_gst"]');
    return Boolean(box && box.checked);
  }

  roundToRupee() {
    const box = this.form.querySelector('[name="round_to_rupee"]');
    return Boolean(box && box.checked);
  }

  compute(line) {
    const qty = Math.max(0, parseInt(line.qty, 10) || 0);
    const unit = line.unit_price || 0;
    const discount = Math.max(0, line.discount || 0);
    const rate = this.taxEnabled ? Math.max(0, parseInt(line.gst_rate_bp, 10) || 0) : 0;
    const gross = Math.max(0, qty * unit - discount);
    const taxable =
      this.pricesIncludeGst() && rate ? mulDivRound(gross, 10000, 10000 + rate) : gross;
    let cgst = 0;
    let sgst = 0;
    let igst = 0;
    if (rate) {
      if (this.interstate()) {
        igst = mulDivRound(taxable, rate, 10000);
      } else {
        cgst = mulDivRound(taxable, rate, 20000);
        sgst = cgst;
      }
    }
    return { qty, taxable, cgst, sgst, igst, total: taxable + cgst + sgst + igst };
  }

  totals() {
    const sum = { taxable: 0, cgst: 0, sgst: 0, igst: 0, qty: 0 };
    this.lines.forEach((line) => {
      const calc = this.compute(line);
      sum.taxable += calc.taxable;
      sum.cgst += calc.cgst;
      sum.sgst += calc.sgst;
      sum.igst += calc.igst;
      sum.qty += calc.qty;
    });
    const subtotal = sum.taxable + sum.cgst + sum.sgst + sum.igst;
    let roundOff = 0;
    if (this.roundToRupee()) {
      const remainder = ((subtotal % 100) + 100) % 100;
      roundOff = remainder < 50 ? -remainder : 100 - remainder;
    }
    return Object.assign(sum, { subtotal, roundOff, grand: subtotal + roundOff });
  }

  /* -- mutation -- */

  addProduct(product, qty) {
    const existing = this.lines.find(
      (line) => line.product_id === product.id && !product.is_serialized
    );
    if (existing) {
      existing.qty = (parseInt(existing.qty, 10) || 0) + (qty || 1);
      this.render();
      this.flash(existing);
      return;
    }
    const price =
      this.mode === 'purchase' ? product.cost_price_paise : product.sale_price_paise;
    this.lines.push({
      product_id: product.id,
      sku: product.sku,
      name: product.name,
      unit: product.unit || 'Nos',
      is_serialized: Boolean(product.is_serialized),
      warranty_months: product.warranty_months || 0,
      qty: qty || 1,
      unit_price: price || 0,
      gst_rate_bp: product.gst_rate_bp || 0,
      hsn_code: product.hsn_code || '',
      discount: 0,
      serials: '',
      description: '',
    });
    this.render();
    this.flash(this.lines[this.lines.length - 1]);
  }

  flash(line) {
    const index = this.lines.indexOf(line);
    // Serial-tracked lines render as two <tr>s, so the row is found by index, not position.
    const row = this.body.querySelector('[data-line-index="' + index + '"]');
    if (!row) return;
    row.style.transition = 'background .5s';
    row.style.background = 'var(--ok-bg)';
    setTimeout(() => {
      row.style.background = '';
    }, 550);
  }

  remove(index) {
    this.lines.splice(index, 1);
    this.render();
  }

  /* -- rendering -- */

  render() {
    if (!this.body) return;
    this.body.textContent = '';
    this.lines.forEach((line, index) => {
      this.body.appendChild(this.renderRow(line, index));
    });
    if (!this.lines.length) {
      const label = this.mode === 'service' ? 'No parts used.' : 'No items yet.';
      this.body.appendChild(
        el('tr', {}, [
          el('td', {
            colspan: '9',
            class: 'empty',
            text: label + ' Scan a barcode or search above.',
          }),
        ])
      );
    }
    this.renderTotals();
    this.serialise();
  }

  renderRow(line, index) {
    const calc = this.compute(line);
    // Two update styles on purpose. `live` keeps the caret where it is while the shopkeeper
    // types a quantity — a full re-render would replace the input and lose focus. `full`
    // is for changes that alter the row's structure (GST slab, serial count).
    const live = (key, cast) => (event) => {
      line[key] = cast ? cast(event.target.value) : event.target.value;
      this.refreshDerived();
    };
    const full = (key, cast) => (event) => {
      line[key] = cast ? cast(event.target.value) : event.target.value;
      this.render();
    };

    const cells = [];
    cells.push(
      el('td', {}, [
        el('strong', { text: line.name }),
        el('small', { text: line.sku + (line.unit ? ' · ' + line.unit : '') }),
        line.is_serialized
          ? el('span', {
              class: 'pill pill-info',
              text: 'serial tracked',
            })
          : null,
      ].filter(Boolean))
    );

    if (this.mode !== 'service') {
      cells.push(
        el('td', { class: 'cell-num' }, [
          el('input', {
            class: 'w-hsn',
            value: line.hsn_code,
            'aria-label': 'HSN code',
            oninput: live('hsn_code'),
          }),
        ])
      );
    }

    cells.push(
      el('td', { class: 'cell-num' }, [
        el('input', {
          type: 'number',
          min: '1',
          step: '1',
          class: 'w-qty',
          value: line.qty,
          'aria-label': 'Quantity',
          oninput: live('qty'),
        }),
      ])
    );

    cells.push(
      el('td', { class: 'cell-num' }, [
        el('input', {
          type: 'text',
          inputmode: 'decimal',
          class: 'w-rate',
          value: (line.unit_price / 100).toFixed(2),
          'aria-label': 'Rate',
          oninput: live('unit_price', toPaise),
        }),
      ])
    );

    if (this.mode === 'sale') {
      cells.push(
        el('td', { class: 'cell-num' }, [
          el('input', {
            type: 'text',
            inputmode: 'decimal',
            class: 'w-rate',
            value: (line.discount / 100).toFixed(2),
            'aria-label': 'Discount',
            oninput: live('discount', toPaise),
          }),
        ])
      );
    }

    if (this.mode !== 'service') {
      const select = el('select', {
        class: 'w-gst',
        'aria-label': 'GST rate',
        onchange: full('gst_rate_bp', (v) => parseInt(v, 10) || 0),
      });
      const slabs = GST_SLABS.includes(Number(line.gst_rate_bp))
        ? GST_SLABS
        : GST_SLABS.concat([Number(line.gst_rate_bp)]).sort((a, b) => a - b);
      slabs.forEach((bp) => {
        const option = el('option', { value: String(bp), text: rateLabel(bp) });
        if (Number(bp) === Number(line.gst_rate_bp)) option.selected = true;
        select.appendChild(option);
      });
      cells.push(el('td', {}, [select]));
    }

    cells.push(
      el('td', { class: 'line-total', 'data-line-total': String(index), text: fmtPaise(calc.total) })
    );
    cells.push(
      el('td', { class: 'actions' }, [
        el('button', {
          type: 'button',
          class: 'btn btn-sm btn-danger',
          text: '×',
          title: 'Remove line',
          'aria-label': 'Remove ' + line.name,
          onclick: () => this.remove(index),
        }),
      ])
    );

    const row = el('tr', { 'data-line-index': String(index) }, cells);

    if (line.is_serialized) {
      const cell = el('td', { colspan: String(cells.length) }, [
        el('label', { 'data-serial-label': String(index), text: this.serialLabel(line) }),
        el('textarea', {
          class: 'serialbox',
          rows: '2',
          placeholder: 'One per line, or comma separated. Scan them straight in.',
          oninput: (event) => {
            line.serials = event.target.value;
            this.refreshDerived();
          },
        }),
      ]);
      cell.querySelector('textarea').value = line.serials || '';
      const serialRow = el('tr', { class: 'serialrow', 'data-serial-row': String(index) }, [cell]);
      if (!this.serialsComplete(line)) row.classList.add('needs-serials');
      const fragment = document.createDocumentFragment();
      fragment.appendChild(row);
      fragment.appendChild(serialRow);
      return fragment;
    }
    return row;
  }

  serialList(line) {
    return String(line.serials || '')
      .split(/[\n,;]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  serialsComplete(line) {
    return this.serialList(line).length === (parseInt(line.qty, 10) || 0);
  }

  serialLabel(line) {
    return (
      'Serial numbers (' +
      this.serialList(line).length +
      ' of ' +
      (parseInt(line.qty, 10) || 0) +
      ')'
    );
  }

  /** Update only the numbers that follow from the model, leaving inputs untouched. */
  refreshDerived() {
    this.lines.forEach((line, index) => {
      const calc = this.compute(line);
      const cell = this.body.querySelector('[data-line-total="' + index + '"]');
      if (cell) cell.textContent = fmtPaise(calc.total);
      if (line.is_serialized) {
        const label = this.body.querySelector('[data-serial-label="' + index + '"]');
        if (label) label.textContent = this.serialLabel(line);
        const row = this.body.querySelector('[data-line-index="' + index + '"]');
        if (row) row.classList.toggle('needs-serials', !this.serialsComplete(line));
      }
    });
    this.renderTotals();
    this.serialise();
  }

  renderTotals() {
    if (!this.totalsBox) return;
    const t = this.totals();
    const rows = [[this.mode === 'service' ? 'Parts total' : 'Taxable value', t.taxable]];
    if (this.taxEnabled) {
      if (t.igst) rows.push(['IGST', t.igst]);
      if (t.cgst) rows.push(['CGST', t.cgst]);
      if (t.sgst) rows.push(['SGST', t.sgst]);
    }
    if (t.roundOff) rows.push(['Round off', t.roundOff]);

    this.totalsBox.textContent = '';
    const table = el('table', { class: 'totals' });
    rows.forEach(([label, value]) => {
      table.appendChild(
        el('tr', {}, [el('td', { text: label }), el('td', { text: fmtPaise(value) })])
      );
    });
    table.appendChild(
      el('tr', { class: 'grand' }, [
        el('td', { text: 'Total' }),
        el('td', { text: '₹' + fmtPaise(t.grand) }),
      ])
    );
    this.totalsBox.appendChild(table);
    if (this.taxEnabled) {
      const word = this.mode === 'purchase' ? 'purchase' : 'supply';
      this.totalsBox.appendChild(
        el('p', {
          class: 'fineprint',
          text: this.interstate()
            ? 'Inter-state ' + word + ' — IGST at the full rate.'
            : 'Intra-state ' + word + ' — CGST + SGST at half the rate each.',
        })
      );
    }
    const counter = this.form.querySelector('[data-line-count]');
    if (counter) counter.textContent = this.lines.length + ' line(s), ' + t.qty + ' unit(s)';
  }

  serialise() {
    if (!this.hidden) return;
    this.hidden.value = JSON.stringify(
      this.lines.map((line) => {
        const payload = {
          product_id: line.product_id,
          qty: parseInt(line.qty, 10) || 0,
          gst_rate_bp: parseInt(line.gst_rate_bp, 10) || 0,
          hsn_code: line.hsn_code || '',
          description: line.description || '',
        };
        payload[this.priceField] = ((line.unit_price || 0) / 100).toFixed(2);
        if (this.mode === 'sale') payload.discount = ((line.discount || 0) / 100).toFixed(2);
        if (line.is_serialized) {
          payload.serials = this.serialList(line);
          payload.warranty_months = line.warranty_months || 0;
        }
        return payload;
      })
    );
  }

  /* -- input plumbing -- */

  focusScanner() {
    if (this.scanInput) this.scanInput.focus();
  }

  async lookup(code) {
    const term = (code || '').trim();
    if (!term) return;
    try {
      const data = await getJSON('/api/lookup?sku=' + encodeURIComponent(term));
      // /api/lookup answers {found, exact, product} or {found: false, reason, candidates}.
      // Read those names exactly — a wrong key here silently turns every good scan into
      // "no product with that code", which is the one thing the counter cannot live with.
      if (data.found && data.product) {
        this.addProduct(data.product, 1);
      } else if (data.reason === 'ambiguous') {
        toast('More than one product matches "' + term + '". Use the search box.', 'warn');
        if (this.searchInput) {
          this.searchInput.value = term;
          this.searchInput.focus();
          this.search(term);
        }
      } else {
        toast('No product with code "' + term + '".', 'err');
        const link = document.querySelector('[data-new-product]');
        if (link) link.href = '/products/new?sku=' + encodeURIComponent(term);
      }
    } catch (error) {
      toast(error.message, 'err');
    }
  }

  async search(term) {
    if (!this.searchResults) return;
    const query = (term || '').trim();
    if (query.length < 2) {
      this.searchResults.textContent = '';
      return;
    }
    try {
      const data = await getJSON('/api/products/search?q=' + encodeURIComponent(query));
      this.searchResults.textContent = '';
      const matches = data.results || [];
      matches.forEach((product) => {
        this.searchResults.appendChild(
          el('button', {
            type: 'button',
            class: 'btn btn-sm',
            text: product.name + ' · ' + product.sku + ' (' + product.quantity + ')',
            onclick: () => {
              this.addProduct(product, 1);
              this.searchResults.textContent = '';
              if (this.searchInput) this.searchInput.value = '';
              this.focusScanner();
            },
          })
        );
      });
      if (!matches.length) {
        this.searchResults.appendChild(el('span', { class: 'muted', text: 'No match.' }));
      }
    } catch (error) {
      toast(error.message, 'err');
    }
  }

  bind() {
    if (this.scanInput) {
      // The scanner types the code then sends Enter. Enter inside a form would submit it,
      // so it is swallowed here and turned into a lookup instead.
      this.scanInput.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        const code = this.scanInput.value;
        this.scanInput.value = '';
        this.lookup(code);
      });
      this.scanInput.addEventListener('blur', () => {
        // Keep the caret in the scan box unless the user deliberately clicked elsewhere.
        setTimeout(() => {
          const active = document.activeElement;
          const isField =
            active &&
            (active.tagName === 'INPUT' ||
              active.tagName === 'SELECT' ||
              active.tagName === 'TEXTAREA' ||
              active.tagName === 'BUTTON' ||
              active.tagName === 'A');
          if (!isField) this.focusScanner();
        }, 60);
      });
    }

    if (this.searchInput) {
      let timer = null;
      this.searchInput.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => this.search(this.searchInput.value), 220);
      });
      this.searchInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          this.search(this.searchInput.value);
        }
      });
    }

    ['customer_state_code', 'dealer_id', 'prices_include_gst', 'round_to_rupee'].forEach(
      (name) => {
        const field = this.form.querySelector('[name="' + name + '"]');
        if (field) field.addEventListener('change', () => this.render());
      }
    );

    const gstinField = this.form.querySelector('[name="customer_gstin"]');
    const stateField = this.form.querySelector('[name="customer_state_code"]');
    if (gstinField && stateField) {
      // The first two digits of a GSTIN are the state code, so fill the state from it.
      gstinField.addEventListener('input', () => {
        const prefix = gstinField.value.trim().slice(0, 2);
        if (/^\d{2}$/.test(prefix) && stateField.value !== prefix) {
          stateField.value = prefix;
          this.render();
        }
      });
    }

    this.form.addEventListener('submit', (event) => {
      this.serialise();
      if (!this.lines.length && this.mode !== 'service') {
        event.preventDefault();
        toast('Add at least one item before saving.', 'err');
        return;
      }
      const bad = this.lines.find((line) => line.is_serialized && !this.serialsComplete(line));
      if (bad) {
        event.preventDefault();
        toast(
          'Enter exactly ' + bad.qty + ' serial number(s) for ' + bad.name + '.',
          'err'
        );
        return;
      }
      const button = this.form.querySelector('[type="submit"]');
      if (button) button.disabled = true;
    });
  }
}

/* -- purchase bill totals ------------------------------------------------ */
/* The dealer's printed figures are typed in, not calculated. This only warns when they
 * disagree with the line items, and offers to copy our sums across. */

function initPurchaseCheck(form) {
  const box = form.querySelector('[data-bill-check]');
  if (!box) return;
  const names = ['bill_taxable', 'bill_cgst', 'bill_sgst', 'bill_igst', 'bill_round_off', 'bill_total'];
  const fields = {};
  names.forEach((name) => {
    fields[name] = form.querySelector('[name="' + name + '"]');
  });

  function refresh() {
    const typed = {};
    names.forEach((name) => {
      typed[name] = fields[name] ? toPaise(fields[name].value) : 0;
    });
    const sum =
      typed.bill_taxable +
      typed.bill_cgst +
      typed.bill_sgst +
      typed.bill_igst +
      typed.bill_round_off;
    box.textContent = '';
    if (!typed.bill_total) {
      box.appendChild(
        el('span', { class: 'muted', text: 'Enter the totals exactly as printed on the bill.' })
      );
      return;
    }
    const diff = sum - typed.bill_total;
    if (diff === 0) {
      box.appendChild(el('span', { class: 'pill pill-ok', text: 'Bill adds up ✓' }));
    } else {
      box.appendChild(
        el('span', {
          class: 'pill pill-warn',
          text:
            'Taxable + GST + round off is ' +
            fmtPaise(sum) +
            ', but the total says ' +
            fmtPaise(typed.bill_total) +
            ' (off by ' +
            fmtPaise(Math.abs(diff)) +
            ')',
        })
      );
    }
  }

  names.forEach((name) => {
    if (fields[name]) fields[name].addEventListener('input', refresh);
  });

  const copy = form.querySelector('[data-copy-lines]');
  if (copy) {
    copy.addEventListener('click', () => {
      const editor = form._lineEditor;
      if (!editor) return;
      const t = editor.totals();
      if (fields.bill_taxable) fields.bill_taxable.value = (t.taxable / 100).toFixed(2);
      if (fields.bill_cgst) fields.bill_cgst.value = (t.cgst / 100).toFixed(2);
      if (fields.bill_sgst) fields.bill_sgst.value = (t.sgst / 100).toFixed(2);
      if (fields.bill_igst) fields.bill_igst.value = (t.igst / 100).toFixed(2);
      if (fields.bill_round_off) fields.bill_round_off.value = '0.00';
      if (fields.bill_total) fields.bill_total.value = (t.subtotal / 100).toFixed(2);
      refresh();
    });
  }
  refresh();
}

/* -- warranty / serial scan boxes on non-editor pages -------------------- */

function initPlainScan() {
  document.querySelectorAll('[data-scan-submit]').forEach((input) => {
    input.focus();
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && input.form) {
        // Let it through: on a search page, Enter submitting the form is the right thing.
        input.form.requestSubmit();
      }
    });
  });
}

/* -- confirm-before-destructive ----------------------------------------- */

function initConfirms() {
  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
}

/* -- boot ---------------------------------------------------------------- */

document.addEventListener('DOMContentLoaded', () => {
  initChrome();
  initStackLabels();
  initPrintButtons();
  initConfirms();
  initPlainScan();
  document.querySelectorAll('form[data-line-editor]').forEach((form) => {
    const editor = new LineEditor(form);
    form._lineEditor = editor;
    initPurchaseCheck(form);
  });
});
