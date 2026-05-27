/* AssetFilter — shared controller for the asset_filter_bar.html macro.
 *
 * Used by pages that embed the macro and want a tidy, page-local way to
 * read the filter state, build query params for /api/assets/page, and be
 * notified when any filter changes.
 *
 * The assets-library page (assets.html) has its own bespoke controller
 * inline; this class is currently only consumed by the slideshow builder.
 * That's fine — both drive the same DOM contract from the macro.
 *
 * Usage:
 *   const filter = new AssetFilter({
 *       prefix: 'ssfilter',
 *       fixedTypes: ['image', 'video'],   // not user-selectable
 *       syncUrl: false,                   // don't touch window.history
 *       debounceMs: 300,
 *       onChange: (state, params) => { ... refetch ... },
 *   });
 *   filter.start();                       // wires listeners + initial render
 *   const qs = filter.buildApiParams();   // URLSearchParams
 */
(function () {
    'use strict';

    function debounce(fn, ms) {
        let t = null;
        return function (...args) {
            if (t) clearTimeout(t);
            t = setTimeout(() => fn.apply(this, args), ms);
        };
    }

    class AssetFilter {
        constructor(opts) {
            opts = opts || {};
            this.prefix = opts.prefix || 'filter';
            this.fixedTypes = Array.isArray(opts.fixedTypes) ? opts.fixedTypes.slice() : null;
            this.syncUrl = opts.syncUrl !== false;       // default true
            this.debounceMs = opts.debounceMs || 300;
            this.onChange = typeof opts.onChange === 'function' ? opts.onChange : () => {};
            this.pageSize = opts.pageSize || 50;
            this.defaultOrder = opts.defaultOrder || '-uploaded_at';

            this.state = {
                q: '',
                type: '',
                group_id: '',
                uploader_id: '',
                tag_id: '',
                date_days: '',
                order: this.defaultOrder,
            };
        }

        _el(name) {
            return document.getElementById(this.prefix + '-' + name);
        }

        _readInitial() {
            // Pull current values from the inputs the macro rendered, falling
            // back to URL params when syncUrl is enabled.
            const url = this.syncUrl ? new URLSearchParams(window.location.search) : null;
            const fields = ['q', 'type', 'group_id', 'uploader_id', 'tag_id', 'date_days'];
            for (const f of fields) {
                let v = '';
                if (url) v = url.get(f) || '';
                const elName = f === 'group_id' ? 'group'
                             : f === 'uploader_id' ? 'uploader'
                             : f === 'tag_id' ? 'tag'
                             : f === 'date_days' ? 'date'
                             : f;
                const el = this._el(elName);
                if (el && !v) v = el.value || '';
                if (el && v) el.value = v;
                this.state[f] = v;
            }
            if (url) {
                const o = url.get('order');
                if (o) this.state.order = o;
            }
        }

        _syncUrl() {
            if (!this.syncUrl) return;
            const p = new URLSearchParams();
            const s = this.state;
            if (s.q) p.set('q', s.q);
            if (s.type) p.set('type', s.type);
            if (s.group_id) p.set('group_id', s.group_id);
            if (s.uploader_id) p.set('uploader_id', s.uploader_id);
            if (s.tag_id) p.set('tag_id', s.tag_id);
            if (s.date_days) p.set('date_days', s.date_days);
            if (s.order && s.order !== this.defaultOrder) p.set('order', s.order);
            const qs = p.toString();
            const newUrl = window.location.pathname + (qs ? '?' + qs : '');
            if (window.location.pathname + window.location.search !== newUrl) {
                window.history.replaceState({}, '', newUrl);
            }
        }

        isAnyFilterActive() {
            const s = this.state;
            return !!(s.q || s.type || s.group_id || s.uploader_id || s.tag_id || s.date_days);
        }

        _updateClearVisibility() {
            const btn = this._el('clear-btn');
            if (btn) btn.style.display = this.isAnyFilterActive() ? '' : 'none';
        }

        getState() {
            return Object.assign({}, this.state);
        }

        /**
         * Build URLSearchParams for /api/assets/page. Does NOT include
         * `cursor` or `page_size` — callers add those as appropriate.
         */
        buildApiParams() {
            const p = new URLSearchParams();
            const s = this.state;
            if (s.q) p.set('q', s.q);
            // fixedTypes always applied; if user picked one too it overrides.
            if (s.type) {
                p.append('type', s.type);
            } else if (this.fixedTypes && this.fixedTypes.length) {
                for (const t of this.fixedTypes) p.append('type', t);
            }
            if (s.group_id) p.append('group_id', s.group_id);
            if (s.uploader_id) p.append('uploader_id', s.uploader_id);
            if (s.tag_id) p.append('tag_id', s.tag_id);
            if (s.date_days) {
                const since = new Date(Date.now() - parseInt(s.date_days, 10) * 86400 * 1000);
                p.set('uploaded_after', since.toISOString());
            }
            p.set('order', s.order || this.defaultOrder);
            return p;
        }

        clearFilters() {
            this.state.q = '';
            this.state.type = '';
            this.state.group_id = '';
            this.state.uploader_id = '';
            this.state.tag_id = '';
            this.state.date_days = '';
            for (const name of ['q', 'type', 'group', 'uploader', 'tag', 'date']) {
                const el = this._el(name);
                if (el) el.value = '';
            }
            this._updateClearVisibility();
            this._syncUrl();
            this.onChange(this.getState(), this.buildApiParams());
        }

        _wireListeners() {
            const fire = () => {
                this._updateClearVisibility();
                this._syncUrl();
                this.onChange(this.getState(), this.buildApiParams());
            };

            const qEl = this._el('q');
            if (qEl) {
                const debounced = debounce(() => {
                    this.state.q = qEl.value.trim();
                    fire();
                }, this.debounceMs);
                qEl.addEventListener('input', debounced);
            }

            const wireSelect = (elName, stateKey) => {
                const el = this._el(elName);
                if (!el) return;
                el.addEventListener('change', () => {
                    this.state[stateKey] = el.value || '';
                    fire();
                });
            };
            wireSelect('type', 'type');
            wireSelect('group', 'group_id');
            wireSelect('uploader', 'uploader_id');
            wireSelect('tag', 'tag_id');
            wireSelect('date', 'date_days');

            const clearBtn = this._el('clear-btn');
            if (clearBtn) {
                clearBtn.addEventListener('click', () => this.clearFilters());
            }
        }

        /**
         * Read initial state from inputs/URL, wire listeners, update the
         * clear-button visibility. Does NOT fire onChange — the caller's
         * initial fetch is its own responsibility.
         */
        start() {
            this._readInitial();
            this._wireListeners();
            this._updateClearVisibility();
            return this;
        }
    }

    window.AssetFilter = AssetFilter;
})();
