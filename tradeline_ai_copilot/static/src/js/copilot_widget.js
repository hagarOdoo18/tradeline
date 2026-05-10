/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState } from "@odoo/owl";

class CopilotBase extends Component {
    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({
            open: this.props.embedded || false,
            loading: false,
            input: "",
            messages: [],
            conversationId: null,
            mode: this.props.embedded ? "fullscreen" : "compact",
            provider: "openai",
            model: "",
            queryMeta: null,
        });
    }

    get rootClass() {
        if (this.props.embedded) {
            return "tl-ai-copilot tl-ai-copilot--embedded";
        }
        return `tl-ai-copilot tl-ai-copilot--${this.state.mode}`;
    }

    toggleOpen() {
        this.state.open = !this.state.open;
    }

    switchMode(mode) {
        this.state.mode = mode;
        this.state.open = true;
    }

    onInput(ev) {
        this.state.input = ev.target.value;
    }

    _buildContext() {
        return {
            page: window.location.hash || "",
        };
    }

    async sendMessage() {
        const content = (this.state.input || "").trim();
        if (!content || this.state.loading) {
            return;
        }

        this.state.messages.push({ role: "user", content });
        this.state.input = "";
        this.state.loading = true;

        try {
            const response = await this.orm.call("ai.copilot.ui", "chat_send", [
                {
                    message: content,
                    conversation_id: this.state.conversationId,
                    context: this._buildContext(),
                    provider: this.state.provider,
                    model: this.state.model || undefined,
                },
            ]);
            this.state.conversationId = response.conversation_id;
            this.state.queryMeta = response.query_meta || null;
            this.state.messages.push({
                role: "assistant",
                blocks: response.blocks || [],
            });
        } catch (error) {
            this.notification.add(error?.message || "Failed to send message", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    async exportAgain(fileType) {
        if (!this.state.queryMeta) {
            this.notification.add("No query metadata available for export.", { type: "warning" });
            return;
        }
        this.state.loading = true;
        try {
            const result = await this.orm.call("ai.copilot.ui", "export_file", [
                {
                    file_type: fileType,
                    query_meta: this.state.queryMeta,
                    conversation_id: this.state.conversationId,
                },
            ]);
            if (result?.url) {
                window.open(result.url, "_blank");
            }
        } catch (error) {
            this.notification.add(error?.message || "Export failed", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.sendMessage();
        }
    }

    openDownload(url) {
        if (url) {
            window.open(url, "_blank");
        }
    }

    normalizeValue(value) {
        if (Array.isArray(value)) {
            return value.length > 1 ? value[1] : value[0];
        }
        if (value === null || value === undefined) {
            return "-";
        }
        return value;
    }
}

class CopilotFloatingWidget extends CopilotBase {}
CopilotFloatingWidget.template = "tradeline_ai_copilot.FloatingWidget";
CopilotFloatingWidget.props = { embedded: { type: Boolean, optional: true } };
CopilotFloatingWidget.defaultProps = { embedded: false };

class CopilotWorkspaceAction extends CopilotBase {}
CopilotWorkspaceAction.template = "tradeline_ai_copilot.WorkspaceAction";
CopilotWorkspaceAction.props = {};

registry.category("main_components").add("tradeline_ai_copilot_widget", {
    Component: CopilotFloatingWidget,
});

registry.category("actions").add("tradeline_ai_copilot.workspace_action", CopilotWorkspaceAction);

