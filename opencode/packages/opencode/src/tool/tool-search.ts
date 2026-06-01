import { Effect, Schema } from "effect"
import * as Tool from "./tool"
import { ToolActivation } from "@/session/tool-activation"
import { matchClusters, formatLoaded, familiesList } from "./tool-search-clusters"

// Experimental on-demand tool loading (OPENCODE_EXPERIMENTAL_TOOL_SEARCH).
// Most tool families are deferred — removed from the request to save context —
// and discovered via this tool. It searches the deferred catalog, activates the
// matching cluster(s) for the session, and returns their full descriptions so
// they're usable on the next step. Registered in tool/registry.ts behind the flag.

export const Parameters = Schema.Struct({
  query: Schema.String,
})

const DESCRIPTION = [
  "Search for and load tools that aren't currently loaded.",
  "",
  "Most capabilities (browsing the web, scheduling/monitoring, desktop control,",
  "web search, file/shell edits, subagents) are NOT loaded by default to save",
  "context. When you need one, call tool_search with a short description of the",
  "capability — the whole matching family loads and becomes callable on your next step.",
  "",
  'Examples: tool_search("open and click a web page") · tool_search("schedule a reminder")',
  '· tool_search("search the web") · tool_search("edit a file").',
].join("\n")

export const ToolSearchTool = Tool.define(
  "tool_search",
  Effect.succeed({
    description: DESCRIPTION,
    parameters: Parameters,
    execute: (args: { query: string }, ctx: Tool.Context) =>
      Effect.sync(() => {
        const catalog = ToolActivation.getCatalog(ctx.sessionID)
        const matched = matchClusters(args.query, catalog)
        if (matched.length === 0) {
          return {
            title: "tool_search: no match",
            output: [
              `No tool family matched "${args.query}". Available families:`,
              "",
              familiesList(catalog),
              "",
              "Try again with a keyword from one of these.",
            ].join("\n"),
            metadata: {},
          }
        }
        ToolActivation.activate(ctx.sessionID, matched)
        return {
          title: `tool_search: loaded ${matched.length}`,
          output: formatLoaded(matched, catalog),
          metadata: {},
        }
      }),
  }),
)
