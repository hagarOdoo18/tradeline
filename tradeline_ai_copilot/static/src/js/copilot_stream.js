/** @odoo-module **/

// Reads SSE with incremental UTF-8 decoding, including frames split across chunks.
export async function readReportStream(response, onEvent) {
    if (!response.ok || !response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
        throw new Error("The reporting connection could not be opened. Please reload Odoo and try again.");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let complete = false;
    const consume = () => {
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const data = frame.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
            if (!data) { continue; }
            const event = JSON.parse(data);
            if (event.type === "error") { throw new Error(event.message || "The report was interrupted."); }
            if (event.type === "done") { complete = true; }
            onEvent(event);
        }
    };
    try {
        while (true) {
            const { value, done } = await reader.read();
            buffer += decoder.decode(value, { stream: !done });
            buffer = buffer.replace(/\r\n/g, "\n");
            if (buffer.length > 4_000_000) { throw new Error("The report exceeded the response limit."); }
            consume();
            if (done) { break; }
        }
        if (!complete) { throw new Error("The response was interrupted before the report finished."); }
    } finally {
        if (!complete) { await reader.cancel().catch(() => {}); }
        reader.releaseLock();
    }
}
