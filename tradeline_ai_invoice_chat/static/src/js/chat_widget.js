/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState, useRef, onMounted, onWillStart, markup } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc as rpcFn } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

// ---------------------------------------------------------------------------
// Text helpers
// ---------------------------------------------------------------------------

const ARABIC_RE = /[؀-ۿݐ-ݿ]/g;

/** True when the text is mostly Arabic -> the bubble is rendered RTL. */
export function isRtl(text) {
    if (!text) {
        return false;
    }
    const arabic = (text.match(ARABIC_RE) || []).length;
    const letters = (text.match(/[A-Za-z؀-ۿݐ-ݿ]/g) || []).length;
    return letters > 0 && arabic / letters > 0.35;
}

function escapeHtml(text) {
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

/** Inline markdown: **bold**, *italic*, `code`. Input must already be escaped. */
function inlineFormat(text) {
    return text
        .replace(/`([^`]+)`/g, '<code class="o_ai_inline_code">$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|\s)\*([^*\n]+)\*(?=\s|$|[.,!?؟،])/g, "$1<em>$2</em>");
}

function isTableRow(line) {
    return /^\s*\|.*\|\s*$/.test(line);
}

function isTableDivider(line) {
    return /^\s*\|[\s:|-]+\|\s*$/.test(line);
}

function splitRow(line) {
    return line
        .trim()
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((cell) => cell.trim());
}

function looksNumeric(cell) {
    return /^[-+]?[\d,.\s]+%?$/.test(cell.replace(/<[^>]+>/g, "").trim());
}

/**
 * Render an assistant / user message: markdown tables, lists, headings, code
 * blocks and the fixed-width tables produced by the pinned reports.
 */
export function renderMessage(text) {
    if (!text) {
        return "";
    }
    const lines = escapeHtml(text).split("\n");
    const out = [];
    let index = 0;

    while (index < lines.length) {
        const line = lines[index];

        // ---- fenced code block ------------------------------------------
        if (/^\s*```/.test(line)) {
            const buffer = [];
            index += 1;
            while (index < lines.length && !/^\s*```/.test(lines[index])) {
                buffer.push(lines[index]);
                index += 1;
            }
            index += 1;
            out.push(`<pre class="o_ai_code" dir="ltr">${buffer.join("\n")}</pre>`);
            continue;
        }

        // ---- markdown table ---------------------------------------------
        if (isTableRow(line) && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
            const head = splitRow(line);
            index += 2;
            const body = [];
            while (index < lines.length && isTableRow(lines[index])) {
                body.push(splitRow(lines[index]));
                index += 1;
            }
            const headHtml = head
                .map((cell) => `<th>${inlineFormat(cell)}</th>`)
                .join("");
            const bodyHtml = body
                .map((row) => {
                    const cells = row
                        .map((cell) => {
                            const cls = looksNumeric(cell) ? ' class="o_ai_num"' : "";
                            return `<td${cls}>${inlineFormat(cell)}</td>`;
                        })
                        .join("");
                    return `<tr>${cells}</tr>`;
                })
                .join("");
            out.push(
                `<div class="o_ai_table_wrap"><table class="o_ai_table">` +
                    `<thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`
            );
            continue;
        }

        // ---- bullet / numbered lists -------------------------------------
        if (/^\s*[-*•]\s+\S/.test(line)) {
            const items = [];
            while (index < lines.length && /^\s*[-*•]\s+\S/.test(lines[index])) {
                items.push(inlineFormat(lines[index].replace(/^\s*[-*•]\s+/, "")));
                index += 1;
            }
            out.push(`<ul class="o_ai_list">${items.map((i) => `<li>${i}</li>`).join("")}</ul>`);
            continue;
        }
        if (/^\s*\d+[.)]\s+\S/.test(line)) {
            const items = [];
            while (index < lines.length && /^\s*\d+[.)]\s+\S/.test(lines[index])) {
                items.push(inlineFormat(lines[index].replace(/^\s*\d+[.)]\s+/, "")));
                index += 1;
            }
            out.push(`<ol class="o_ai_list">${items.map((i) => `<li>${i}</li>`).join("")}</ol>`);
            continue;
        }

        index += 1;

        // ---- headings and separators -------------------------------------
        const trimmed = line.trim();
        if (/^={3,}.*={3,}$/.test(trimmed)) {
            out.push(
                `<h6 class="o_ai_section_header">${trimmed.replace(/={3,}/g, "").trim()}</h6>`
            );
            continue;
        }
        if (/^#{1,6}\s+/.test(trimmed)) {
            out.push(`<h6 class="o_ai_section_header">${inlineFormat(trimmed.replace(/^#{1,6}\s+/, ""))}</h6>`);
            continue;
        }
        if (/^[-=_]{4,}$/.test(trimmed)) {
            out.push('<hr class="o_ai_rule"/>');
            continue;
        }
        if (!trimmed) {
            out.push('<div class="o_ai_gap"></div>');
            continue;
        }
        // ---- fixed-width report row --------------------------------------
        if (/\s{3,}\S/.test(line)) {
            out.push(`<code class="o_ai_table_row" dir="ltr">${line}</code>`);
            continue;
        }
        out.push(`<div class="o_ai_line">${inlineFormat(line)}</div>`);
    }
    return out.join("");
}

// ---------------------------------------------------------------------------
// Suggested questions, grouped so the welcome screen reads like a menu
// ---------------------------------------------------------------------------
const SUGGESTION_GROUPS = [
    {
        icon: "fa-file-text-o",
        title: _t("Invoicing"),
        items: [
            _t("Invoice summary for this month"),
            _t("List overdue invoices"),
            _t("Payments by branch and journal"),
        ],
    },
    {
        icon: "fa-line-chart",
        title: _t("Sales"),
        items: [
            _t("Top selling products this year"),
            _t("Top 10 customers this quarter"),
            _t("Sales orders by status this month"),
        ],
    },
    {
        icon: "fa-cubes",
        title: _t("Inventory & POS"),
        items: [
            _t("Stock on hand per product"),
            _t("POS sales by session today"),
            _t("Purchase orders by vendor last month"),
        ],
    },
];

// ---------------------------------------------------------------------------
// Chat Client Component
// ---------------------------------------------------------------------------

class AiInvoiceChatClient extends Component {
    static template = "tradeline_ai_invoice_chat.ChatClient";
    static props = {};

    setup() {
        // Odoo 17.2+ exposes rpc as a plain function; older builds as a service.
        try {
            this.rpc = useService("rpc");
        } catch {
            this.rpc = rpcFn;
        }
        this.notification = useService("notification");
        this.messagesRef = useRef("messagesContainer");
        this.inputRef = useRef("inputArea");

        this.suggestionGroups = SUGGESTION_GROUPS;
        this.quickAsks = [
            _t("Invoice summary this month"),
            _t("Overdue invoices"),
            _t("Top products this year"),
            _t("Stock on hand"),
        ];

        this.state = useState({
            sessions: [],
            sessionId: null,
            sessionName: "Tradeline AI Assistant",
            messages: [],
            loading: false,
            showQuickAsks: false,
        });

        onWillStart(async () => {
            await this._loadSessions();
        });

        onMounted(() => {
            this._scrollToBottom();
            this._focusInput();
        });
    }

    // -----------------------------------------------------------------------
    // Rendering helpers used by the template
    // -----------------------------------------------------------------------
    renderContent(text) {
        return markup(renderMessage(text));
    }

    directionOf(text) {
        return isRtl(text) ? "rtl" : "ltr";
    }

    formatTime(value) {
        const when = value ? new Date(value) : new Date();
        return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    get greeting() {
        const hour = new Date().getHours();
        if (hour < 12) {
            return _t("Good morning");
        }
        if (hour < 18) {
            return _t("Good afternoon");
        }
        return _t("Good evening");
    }

    // -----------------------------------------------------------------------
    // Session management
    // -----------------------------------------------------------------------
    async _loadSessions() {
        try {
            this.state.sessions = (await this.rpc("/ai_invoice_chat/sessions", {})) || [];
        } catch {
            this.state.sessions = [];
        }
    }

    async newSession() {
        const res = await this.rpc("/ai_invoice_chat/new_session", {});
        this.state.sessionId = res.session_id;
        this.state.sessionName = res.name;
        this.state.messages = [];
        await this._loadSessions();
        this._focusInput();
    }

    async loadSession(sessionId) {
        this.state.sessionId = sessionId;
        const found = this.state.sessions.find((s) => s.id === sessionId);
        this.state.sessionName = found ? found.name : _t("Chat");
        this.state.messages = [];
        try {
            const history = await this.rpc("/ai_invoice_chat/history", {
                session_id: sessionId,
            });
            this.state.messages = history || [];
        } catch {
            this.state.messages = [];
        }
        this._scrollToBottom();
        this._focusInput();
    }

    async clearSession(sessionId) {
        await this.rpc("/ai_invoice_chat/clear", { session_id: sessionId });
        if (this.state.sessionId === sessionId) {
            this.state.messages = [];
        }
        await this._loadSessions();
        this.notification.add(_t("Chat cleared."), { type: "info" });
    }

    async clearCurrentSession() {
        if (this.state.sessionId) {
            await this.clearSession(this.state.sessionId);
        }
    }

    toggleQuickAsks() {
        this.state.showQuickAsks = !this.state.showQuickAsks;
    }

    async copyMessage(text) {
        try {
            await navigator.clipboard.writeText(text);
            this.notification.add(_t("Copied to clipboard."), { type: "success" });
        } catch {
            this.notification.add(_t("Could not copy — select the text manually."), {
                type: "warning",
            });
        }
    }

    // -----------------------------------------------------------------------
    // Messaging
    // -----------------------------------------------------------------------
    async sendMessage() {
        const input = this.inputRef.el;
        if (!input) {
            return;
        }
        const text = input.value.trim();
        if (!text || this.state.loading) {
            return;
        }

        if (!this.state.sessionId) {
            const res = await this.rpc("/ai_invoice_chat/new_session", {});
            this.state.sessionId = res.session_id;
            this.state.sessionName = text.slice(0, 60);
        }

        this.state.messages.push({ role: "user", content: text, time: Date.now() });
        input.value = "";
        input.style.height = "auto";
        this.state.loading = true;
        this.state.showQuickAsks = false;
        this._scrollToBottom();

        try {
            const res = await this.rpc("/ai_invoice_chat/send", {
                session_id: this.state.sessionId,
                message: text,
            });
            this.state.messages.push({
                role: "assistant",
                content: res.reply,
                time: Date.now(),
            });
            if (res.session_name) {
                this.state.sessionName = res.session_name;
            }
            this.state.sessionId = res.session_id;
            await this._loadSessions();
        } catch {
            this.state.messages.push({
                role: "assistant",
                content: _t(
                    "Sorry — I couldn't reach the server just now. Please try again in a moment."
                ),
                time: Date.now(),
            });
        } finally {
            this.state.loading = false;
            this._scrollToBottom();
            this._focusInput();
        }
    }

    async useSuggestion(text) {
        const input = this.inputRef.el;
        if (input) {
            input.value = text;
        }
        await this.sendMessage();
    }

    onKeyDown(event) {
        const input = this.inputRef.el;
        if (input) {
            input.style.height = "auto";
            input.style.height = Math.min(input.scrollHeight, 160) + "px";
        }
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.sendMessage();
        }
    }

    // -----------------------------------------------------------------------
    // Utilities
    // -----------------------------------------------------------------------
    _scrollToBottom() {
        const el = this.messagesRef.el;
        if (el) {
            setTimeout(() => {
                el.scrollTop = el.scrollHeight;
            }, 50);
        }
    }

    _focusInput() {
        const input = this.inputRef.el;
        if (input) {
            setTimeout(() => input.focus(), 100);
        }
    }
}

registry.category("actions").add("ai_invoice_chat", AiInvoiceChatClient);
