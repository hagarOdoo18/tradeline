/** @odoo-module **/

import { Navbar } from "@point_of_sale/app/navbar/navbar";
import { patch } from "@web/core/utils/patch";
import { onMounted, onPatched, onWillUnmount } from "@odoo/owl";

patch(Navbar.prototype, {
    setup() {
        super.setup(...arguments);
        this._lotSerialSearchInputRef = null;
        this._lotSerialSearchInputEl = null;
        this._lotSerialSearchKeydownHandler = (event) => {
            void this._onLotSerialSearchKeydown(event);
        };

        onMounted(() => {
            this._syncLotSerialSearchInputListener();
        });

        onPatched(() => {
            this._syncLotSerialSearchInputListener();
        });

        onWillUnmount(() => {
            this._removeLotSerialSearchInputListener();
        });
    },

    setSearchInputRef(ref) {
        this._lotSerialSearchInputRef = ref;
    },

    _syncLotSerialSearchInputListener() {
        const inputEl = this._lotSerialSearchInputRef?.el;
        if (inputEl === this._lotSerialSearchInputEl) {
            return;
        }

        this._removeLotSerialSearchInputListener();
        this._lotSerialSearchInputEl = inputEl || null;
        this._lotSerialSearchInputEl?.addEventListener("keydown", this._lotSerialSearchKeydownHandler);
    },

    _removeLotSerialSearchInputListener() {
        if (this._lotSerialSearchInputEl) {
            this._lotSerialSearchInputEl.removeEventListener(
                "keydown",
                this._lotSerialSearchKeydownHandler
            );
            this._lotSerialSearchInputEl = null;
        }
    },

    async _onLotSerialSearchKeydown(event) {
        if (
            event.key !== "Enter" ||
            event.defaultPrevented ||
            event.isComposing ||
            event.altKey ||
            event.ctrlKey ||
            event.metaKey ||
            event.shiftKey
        ) {
            return;
        }

        if (
            !this.pos.config.enable_product_bar_lot_serial_search ||
            this.pos.mainScreen.component?.name !== "ProductScreen"
        ) {
            return;
        }

        // A scanner's Enter can arrive before Input's 500ms model debounce.
        const input = event.currentTarget || this._lotSerialSearchInputEl;
        const searchWord = (input?.value ?? this.pos.searchProductWord ?? "").trim();
        if (!searchWord) {
            return;
        }

        const addExactLotSerialMatch = this.pos._addExactLotSerialSearchMatch;
        if (typeof addExactLotSerialMatch !== "function") {
            return;
        }

        event.preventDefault();
        event.stopPropagation();

        const handled = await addExactLotSerialMatch(searchWord, { skipMinLength: true });
        if (handled && input) {
            // Replace the pending debounced input value as well as the model,
            // otherwise it can restore the just-scanned serial after the add.
            input.value = "";
            input.dispatchEvent(new Event("input", { bubbles: true }));
            this.pos.searchProductWord = "";
        }
    },
});
