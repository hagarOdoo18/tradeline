/** @odoo-module **/
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onMounted, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { user } from "@web/core/user";
import { CopilotChart } from "./copilot_chart";
import { readReportStream } from "./copilot_stream";

export class CopilotBase extends Component {
    static components = { CopilotChart };
    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.messagesRef = useRef("messages");
        this.inputRef = useRef("input");
        this.panelRef = useRef("panel");
        this.autoScroll = true;
        this.textBuffer = "";
        this.state = useState({ open: false, loading: false, historyLoading: false, input: "", messages: [],
            conversationId: null, mode: "side", provider: "", models: {}, history: [], search: "", sidebar: window.innerWidth >= 900,
            enableXlsx: true, enableCsv: true, exportBusy: "", error: "", enableHistory: true,
            streamVersion: 0, status: "", configLoading: false, canAccess: false, showLatest: false });
        onWillStart(async () => {
            try { this.state.canAccess = await user.hasGroup("tradeline_ai_copilot.group_ai_copilot_user"); }
            catch { /* Fail closed without disrupting the underlying Odoo page. */ }
            if (this.embedded && this.state.canAccess) { await this.initialize(); }
        });
        const rememberFocus = event => {
            if (!event.target.closest?.(".tl-ai-copilot, .tl-ai-launcher")) { this.previousFocus = event.target; }
        };
        onMounted(() => document.addEventListener("focusin", rememberFocus));
        onWillUnmount(() => {
            document.removeEventListener("focusin", rememberFocus);
            this.streamController?.abort();
            cancelAnimationFrame(this.textFrame);
        });
        useEffect(() => {
            if (this.autoScroll && this.messagesRef.el) { this.messagesRef.el.scrollTop = this.messagesRef.el.scrollHeight; }
        }, () => [this.state.messages.length, this.state.loading, this.state.historyLoading, this.state.streamVersion]);
    }
    get embedded() { return Boolean(this.props.embedded); }
    get rootClass() { return `tl-ai-copilot tl-ai-copilot--${this.embedded ? "embedded" : this.state.mode}`; }
    get filteredHistory() {
        const query = this.state.search.trim().toLocaleLowerCase();
        return this.state.history.filter(item => item.name.toLocaleLowerCase().includes(query));
    }
    get suggestions() { return ["Compare confirmed sales by branch this month",
        "Show a monthly trend of posted customer invoices this year",
        "Which products have the most available stock?", "Show overdue customer invoices and export to Excel"]; }
    get busy() { return this.state.loading || this.state.historyLoading || this.state.configLoading; }
    async initialize() {
        this.state.configLoading = true;
        try {
            const config = await this.orm.call("ai.copilot.ui", "bootstrap", []);
            Object.assign(this.state, { provider: config.provider, models: config.models,
                enableXlsx: config.enable_xlsx, enableCsv: config.enable_csv, enableHistory: config.enable_history });
            await this.refreshHistory();
        } catch (error) { this.state.error = this.errorText(error); }
        finally { this.state.configLoading = false; }
    }
    errorText(error) { return error?.data?.message || error?.message || "The request could not be completed. Please try again."; }
    normalizeBlocks(blocks) {
        const normalized = [];
        for (let index = 0; index < blocks.length; index++) {
            if (blocks[index].type === "chart" && blocks[index + 1]?.type === "table") {
                normalized.push({ ...blocks[index + 1], chart: blocks[index] });
                index++;
            } else { normalized.push(blocks[index]); }
        }
        return normalized;
    }
    async refreshHistory() { this.state.history = await this.orm.call("ai.copilot.ui", "history", []); }
    async toggleOpen() {
        if (!this.state.canAccess) { return; }
        this.state.open = !this.state.open;
        if (this.state.open) {
            await this.initialize();
        } else if (this.previousFocus?.isConnected) {
            this.previousFocus.focus({ preventScroll: true });
        }
    }
    onPanelKeydown(ev) {
        // Popup keys must never trigger the underlying form's Save/Discard shortcuts.
        ev.stopPropagation();
        if (ev.key === "Escape" && !this.embedded) { ev.preventDefault(); this.toggleOpen(); }
    }
    onMessagesScroll() {
        const el = this.messagesRef.el;
        this.autoScroll = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        this.state.showLatest = !this.autoScroll;
    }
    jumpToLatest() {
        this.autoScroll = true;
        this.state.showLatest = false;
        if (this.messagesRef.el) { this.messagesRef.el.scrollTop = this.messagesRef.el.scrollHeight; }
    }
    useReplyChoice(choice) {
        if (this.busy) { return; }
        this.state.input = choice;
        this.inputRef.el?.focus({ preventScroll: true });
    }
    async copyAnswer(message) {
        const text = (message.blocks || []).filter(block => block.type === "text").map(block => block.content).join("\n\n");
        if (!text) { return; }
        try {
            await navigator.clipboard.writeText(text);
            this.notification.add("Answer copied.", { type: "success" });
        } catch {
            this.notification.add("Copy is unavailable in this browser. Select the answer text to copy it.", { type: "warning" });
        }
    }
    toggleSidebar() { this.state.sidebar = !this.state.sidebar; }
    toggleSize() { this.state.mode = this.state.mode === "fullscreen" ? "side" : "fullscreen"; }
    onInput(ev) { this.state.input = ev.target.value; }
    onSearch(ev) { this.state.search = ev.target.value; }
    onProviderChange(ev) { this.state.provider = ev.target.value; }
    newConversation() {
        if (this.busy) { return; }
        Object.assign(this.state, { conversationId: null, messages: [], input: "", error: "", showLatest: false });
        this.autoScroll = true;
        this.inputRef.el?.focus();
    }
    async loadConversation(id) {
        if (this.busy) { return; }
        this.state.historyLoading = true;
        this.autoScroll = true;
        this.state.showLatest = false;
        this.state.error = "";
        try {
            const result = await this.orm.call("ai.copilot.ui", "load_conversation", [id]);
            Object.assign(this.state, { conversationId: result.id, messages: result.messages.map(message => ({ ...message, blocks: this.normalizeBlocks(message.blocks || []) })),
                provider: result.provider || this.state.provider });
        } catch (error) { this.state.error = this.errorText(error); }
        finally { this.state.historyLoading = false; }
    }
    useSuggestion(text) { this.state.input = text; this.inputRef.el?.focus(); }
    async sendMessage() {
        const content = this.state.input.trim();
        if (!this.state.canAccess || !content || this.busy || !this.state.provider) { return; }
        this.state.messages.push({ role: "user", content });
        const pending = { role: "assistant", streaming: true, blocks: [{ type: "text", content: "" }] };
        this.state.messages.push(pending);
        this.pendingMessage = this.state.messages[this.state.messages.length - 1];
        this.autoScroll = true;
        this.textBuffer = "";
        Object.assign(this.state, { input: "", error: "", loading: true, showLatest: false, status: "Understanding your question..." });
        this.streamController = new AbortController();
        let responseSaved = false;
        try {
            const form = new FormData();
            form.append("csrf_token", odoo.csrf_token);
            form.append("payload", JSON.stringify({ message: content, conversation_id: this.state.conversationId,
                provider: this.state.provider, company_ids: user.context.allowed_company_ids }));
            const response = await fetch("/ai_copilot/chat/stream", { method: "POST", credentials: "same-origin",
                body: form, signal: this.streamController.signal, headers: { Accept: "text/event-stream" } });
            await readReportStream(response, event => {
                if (event.type === "round") {
                    this.textBuffer = "";
                    this.flushStreamText();
                    this.state.status = event.status;
                } else if (event.type === "status") {
                    this.state.status = event.status;
                } else if (event.type === "delta") {
                    this.textBuffer += event.text;
                    this.state.status = "Writing your report...";
                    if (!this.textFrame) {
                        this.textFrame = requestAnimationFrame(() => this.flushStreamText());
                    }
                } else if (event.type === "done") {
                    cancelAnimationFrame(this.textFrame);
                    this.textFrame = null;
                    this.state.conversationId = event.conversation_id;
                    Object.assign(this.pendingMessage, { id: event.message_id, streaming: false,
                        blocks: this.normalizeBlocks(event.blocks || []) });
                    responseSaved = true;
                }
            });
            try { await this.refreshHistory(); } catch { /* Keep the successful answer if sidebar refresh fails. */ }
        } catch (error) {
            if (!responseSaved) {
                this.flushStreamText();
                this.pendingMessage.streaming = false;
                this.pendingMessage.blocks.push({ type: "warning", content: "This report did not finish. Treat any partial text as unverified." });
                if (!this.state.input) { this.state.input = content; }
                this.state.error = error.name === "AbortError" ? "Response stopped." : this.errorText(error);
            }
        } finally {
            cancelAnimationFrame(this.textFrame);
            this.textFrame = null;
            this.state.loading = false;
            this.streamController = null;
            // Completing a response must not interrupt someone typing in an Odoo form.
            if (this.panelRef.el?.contains(document.activeElement)) { this.inputRef.el?.focus({ preventScroll: true }); }
        }
    }
    flushStreamText() {
        this.textFrame = null;
        if (this.pendingMessage?.streaming) { this.pendingMessage.blocks[0].content = this.textBuffer; this.state.streamVersion++; }
    }
    stopResponse() { this.streamController?.abort(); }
    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); this.sendMessage(); }
    }
    normalizeValue(value) {
        if (Array.isArray(value)) { return value.length === 2 && typeof value[1] === "string" ? value[1] : value.join(", "); }
        if (value === null || value === undefined || value === false) { return "—"; }
        if (typeof value === "number") { return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value); }
        return String(value);
    }
    answerParts(content, message) {
        const citations = new Map((message.blocks || []).filter(block => block.type === "web_sources")
            .flatMap(block => block.citations || []).filter(citation => String(citation.url || "").startsWith("https://"))
            .map(citation => [citation.id, citation]));
        return String(content || "").split(/(\[W\d+\])/g).map(text => {
            const citation = citations.get(text.slice(1, -1));
            return { text, url: citation?.url, title: citation?.title };
        });
    }
    sourceCompanies(source) { return (source.companies || []).map(company => company.name).join(", "); }
    sourceFields(source, keys) { return (keys || []).map(key => source.labels?.[key] || key).join(", "); }
    sourceFilter(filter) {
        if (filter.boolean) { return filter.boolean; }
        const operators = { "=": "is", "!=": "is not", ">": "is greater than", "<": "is less than",
            ">=": "is at least", "<=": "is at most", "in": "is one of", "not in": "is not one of", "ilike": "contains", "not ilike": "does not contain" };
        const value = Array.isArray(filter.value) ? filter.value.map(value => this.normalizeValue(value)).join(", ")
            : filter.value === false ? "not set / false" : filter.value === true ? "true" : this.normalizeValue(filter.value);
        return `${filter.field} ${operators[filter.operator] || filter.operator} ${value}`;
    }
    openDownload(url) {
        if (/^\/web\/content\/\d+\?download=1$/.test(url || "")) {
            const anchor = document.createElement("a"); anchor.href = url; anchor.download = ""; anchor.click();
        }
    }
    async exportResult(block, kind) {
        if (this.state.exportBusy) { return; }
        this.state.exportBusy = `${block.result_id}:${kind}`;
        try {
            const response = await this.orm.call("ai.copilot.ui", "export_file", [{ file_type: kind,
                query_meta: block.query_meta, conversation_id: this.state.conversationId }]);
            this.openDownload(response.url);
            if (response.truncated) {
                this.notification.add("The export reached its configured row/group limit. Narrow the filters for complete coverage.", { type: "warning" });
            }
        } catch (error) { this.notification.add(this.errorText(error), { type: "danger" }); }
        finally { this.state.exportBusy = ""; }
    }
}
class CopilotFloatingWidget extends CopilotBase {
    static template = "tradeline_ai_copilot.FloatingWidget";
    static props = { embedded: { type: Boolean, optional: true } };
}
class CopilotWorkspaceAction extends CopilotBase {
    static template = "tradeline_ai_copilot.WorkspaceAction";
    static props = ["*"];
    get embedded() { return true; }
}
registry.category("main_components").add("tradeline_ai_copilot_widget", { Component: CopilotFloatingWidget });
registry.category("actions").add("tradeline_ai_copilot.workspace_action", CopilotWorkspaceAction);
