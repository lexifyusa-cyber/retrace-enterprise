/**
 * Faithful LLM capture for the ReTrace LLM Logs page.
 *
 * A Vercel AI SDK middleware appended last in `wrapLanguageModel`'s chain
 * (see `llm.ts`) snapshots the EXACT post-transform params the provider is
 * about to receive — system prompt, messages, tools with full MCP JSON
 * schemas, sampling params. A top-level `onFinish` callback then POSTs that
 * snapshot plus the accumulated response to the ReTrace backend, which
 * writes one `llm_logs` row.
 *
 * Everything here is best-effort: capture must never disrupt or slow the
 * agent turn, so every path is wrapped in try/catch and failures are
 * swallowed.
 */
import { randomUUID } from "node:crypto"

export type RetraceCaptureMeta = {
  sessionID: string
  modelID: string
  providerID: string
  agentName?: string
}

export type RetraceCaptureRef = {
  params?: unknown
  logID?: string
  startedAt?: number
}

// 1 MB per string/blob — keeps a runaway base64 screenshot or huge tool
// result from bloating the row. The backend caps total payload separately.
const MAX_VALUE_BYTES = 1_000_000

// Deep-clone replacer: drops secrets (`headers` carries bearer tokens),
// drops the non-serializable `abortSignal`, base64-encodes binary file
// payloads so image parts survive, and truncates oversized values.
function captureReplacer(key: string, value: unknown): unknown {
  if (key === "abortSignal" || key === "headers") return undefined
  const lower = key.toLowerCase()
  if (lower === "authorization" || lower === "api_key" || lower === "api-key" || lower.includes("apikey")) {
    return undefined
  }
  if (value instanceof Uint8Array || value instanceof ArrayBuffer) {
    const buf = Buffer.from(value as Uint8Array)
    if (buf.length > MAX_VALUE_BYTES) return "…[truncated binary]"
    return buf.toString("base64")
  }
  if (typeof value === "string" && value.length > MAX_VALUE_BYTES) {
    return value.slice(0, MAX_VALUE_BYTES) + "…[truncated]"
  }
  return value
}

/**
 * Middleware appended LAST in the `wrapLanguageModel` chain so it observes
 * params AFTER provider-specific transforms have run. `transformParams`
 * snapshots a cleaned deep clone onto `ref` and returns `args.params`
 * UNCHANGED — it never mutates the outgoing request.
 */
export function createRetraceCaptureMiddleware(ref: RetraceCaptureRef) {
  return {
    specificationVersion: "v3" as const,
    async transformParams(args: any) {
      try {
        if (args?.type === "stream") {
          ref.logID = randomUUID()
          ref.startedAt = Date.now()
          ref.params = JSON.parse(JSON.stringify(args.params, captureReplacer))
        }
      } catch {
        // Capture is best-effort — never disrupt the LLM call.
      }
      return args.params
    },
  }
}

/**
 * Builds the `streamText` `onFinish` handler. Reads the snapshot left on
 * `ref` by the middleware, pairs it with the response event, and fires a
 * single fire-and-forget POST to the backend capture endpoint.
 */
export function buildRetraceOnFinish(meta: RetraceCaptureMeta, ref: RetraceCaptureRef) {
  return async (event: any): Promise<void> => {
    try {
      const backendURL = process.env["RETRACE_BACKEND_URL"]
      const token = process.env["RETRACE_INTERNAL_TOKEN"]
      if (!backendURL || !token || !ref.params) return

      const body = {
        user_id: process.env["RETRACE_USER_ID"] ?? null,
        session_id: meta.sessionID,
        log_id: ref.logID ?? randomUUID(),
        model_provider: meta.providerID,
        model_name: meta.modelID,
        agent_name: meta.agentName ?? null,
        started_at_ms: ref.startedAt ?? 0,
        completed_at_ms: Date.now(),
        request: ref.params,
        response: {
          text: event?.text ?? "",
          reasoningText: event?.reasoningText ?? "",
          toolCalls: Array.isArray(event?.toolCalls)
            ? event.toolCalls.map((tc: any) => ({
                toolCallId: tc?.toolCallId,
                toolName: tc?.toolName,
                input: tc?.input,
              }))
            : [],
          finishReason: event?.finishReason ?? null,
          usage: event?.usage ?? null,
          warnings: event?.warnings ?? null,
          providerMetadata: event?.providerMetadata ?? null,
        },
        error: null,
      }

      await fetch(`${backendURL}/api/v1/internal/llm/capture`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Internal-Token": token,
        },
        body: JSON.stringify(body),
        keepalive: true,
      })
    } catch {
      // A logging hook must never surface errors into the agent turn.
    }
  }
}
