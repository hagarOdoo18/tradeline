/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState } from "@odoo/owl";

const MODEL_OPTIONS = {
    openai: [
        { value: "", label: "Use default" },
        { value: "gpt-4.1-mini", label: "GPT-4.1 Mini" },
        { value: "gpt-4.1", label: "GPT-4.1" },
        { value: "gpt-4o-mini", label: "GPT-4o Mini" },
        { value: "gpt-4o", label: "GPT-4o" },
    ],
    claude: [
        { value: "", label: "Use default" },
        { value: "claude-3-5-haiku-latest", label: "Claude Haiku" },
        { value: "claude-3-5-sonnet-latest", label: "Claude Sonnet" },
        { value: "claude-3-opus-latest", label: "Claude Opus" },
    ],
};

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
        });
    }

    get rootClass() {
        if (this.props.embedded) {
            return "tl-ai-copilot tl-ai-copilot--embedded";
        }
        return `tl-ai-copilot tl-ai-copilot--${this.state.mode}`;
    }

    get modelOptions() {
        return MODEL_OPTIONS[this.state.provider] || MODEL_OPTIONS.openai;
    }

    toggleOpen() {
        this.state.open = !this.state.open;
    }

    switchMode(mode) {
        this.state.mode = mode;
        this.state.open = true;
    }

    onProviderChange(ev) {
        this.state.provider = ev.target.value || "openai";
        const values = this.modelOptions.map((item) => item.value);
        if (!values.includes(this.state.model)) {
            this.state.model = "";
        }
    }

    onModelChange(ev) {
        this.state.model = ev.target.value || "";
    }

    onInput(ev) {
        this.state.input = ev.target.value;
    }

    _buildContext() {
        return {
            page: window.location.hash || "",
        };
    }

    _assistantMessageFromResponse(response) {
        const blocks = response.blocks || [];
        const downloads = blocks.filter((item) => item.type === "download");
        return {
            role: "assistant",
            blocks,
            downloads,
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
            this.state.messages.push(this._assistantMessageFromResponse(response));
        } catch (error) {
            this.notification.add(error?.message || "Failed to send message", { type: "danger" });
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

