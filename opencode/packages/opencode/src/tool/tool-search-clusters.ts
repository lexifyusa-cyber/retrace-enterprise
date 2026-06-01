// Cluster definitions + pure helpers for the experimental tool-search feature
// (OPENCODE_EXPERIMENTAL_TOOL_SEARCH). Dependency-free so it can be shared by
// llm.ts (activeTools filter), prompt.ts (menu + catalog), and the tool_search
// tool without import cycles.
//
// Model: ALLOWLIST. A small explicit core (`isAlwaysOn`) is advertised on every
// turn; EVERYTHING else defers — grouped into clusters — until the model
// activates it via tool_search. This means user-installed MCPs (ServiceNow,
// Jira, …) and any tool added later defer automatically, instead of leaking
// into every request. Named clusters below just give nice labels + tuned search
// keywords; anything not named falls into its MCP-server cluster, or "other".

import type { CatalogEntry } from "@/session/tool-activation"

// ── Always-on core ───────────────────────────────────────────────────────────
// The knowledge base (`retrace-kb_*`), the discovery tool, todo + question, plus
// two dynamic/internal tools that MUST stay callable: the structured-output tool
// (json_schema mode) and the GitHub-Copilot API-compat no-op.
const CORE_IDS = new Set(["tool_search", "todowrite", "question", "StructuredOutput", "_noop"])

export function isAlwaysOn(id: string): boolean {
  return CORE_IDS.has(id) || id.startsWith("retrace-kb_")
}

// ── Named clusters (labels + tuned keywords) ─────────────────────────────────
export interface ClusterInfo {
  key: string
  label: string
  blurb: string
  keywords: string[]
  /** id belongs to this cluster if it starts with `prefix`… */
  prefix?: string
  /** …or is one of `ids`. */
  ids?: string[]
}

export const CLUSTERS: ClusterInfo[] = [
  {
    key: "browser",
    label: "Browser",
    blurb: "open, click, type on, and read web pages",
    prefix: "retrace-browser_",
    keywords: [
      "web", "webpage", "page", "website", "url", "site", "click", "navigate", "browse",
      "browser", "screenshot", "scroll", "type", "form", "button", "link", "dom", "html",
      "hover", "tab", "login", "fill", "submit",
    ],
  },
  {
    key: "web-search",
    label: "Web search",
    blurb: "search the web or research a topic",
    prefix: "retrace-search_",
    keywords: ["search", "google", "research", "lookup", "internet", "online", "serper", "news", "find"],
  },
  {
    key: "scheduler",
    label: "Scheduler",
    blurb: "schedule, monitor, remind, or set up webhooks",
    prefix: "retrace-scheduler_",
    keywords: [
      "schedule", "scheduled", "monitor", "watch", "remind", "reminder", "cron", "interval",
      "followup", "follow", "webhook", "trigger", "alert", "recurring", "later", "every",
      "notify", "periodic", "cancel",
    ],
  },
  {
    key: "screen",
    label: "Screen control",
    blurb: "click/type on the desktop screen",
    prefix: "retrace-screenops_",
    keywords: ["desktop", "screen", "mouse", "keyboard", "gui", "application", "cursor", "keypress"],
  },
  {
    key: "file-shell",
    label: "File & shell",
    blurb: "read/edit files, run shell commands",
    ids: ["bash", "edit", "write", "glob", "grep", "read"],
    keywords: [
      "file", "files", "edit", "write", "bash", "shell", "command", "terminal", "grep",
      "glob", "code", "directory", "folder", "run", "script", "execute", "cd",
    ],
  },
  {
    key: "subagent",
    label: "Subagents & misc",
    blurb: "background subagents, fetch a URL, load a skill",
    ids: ["task", "task_status", "webfetch", "skill"],
    keywords: ["subagent", "task", "agent", "background", "delegate", "fetch", "download", "skill", "parallel", "spawn"],
  },
]

const BY_KEY = new Map(CLUSTERS.map((c) => [c.key, c]))

// The deferrable cluster a tool belongs to. Callers check `isAlwaysOn` first, so
// this is only meaningful for non-core tools. Returns a named cluster, else the
// tool's MCP-server prefix (so each user-installed MCP becomes its own family),
// else "other". Never returns undefined — everything non-core has a cluster.
export function clusterOf(id: string): string {
  for (const c of CLUSTERS) {
    if (c.prefix && id.startsWith(c.prefix)) return c.key
    if (c.ids && c.ids.includes(id)) return c.key
  }
  // MCP tool ids look like "<server>_<tool>"; server names are hyphenated
  // (e.g. "user-servicenow", "jira-cloud") so the prefix before the first "_"
  // is the server. Group an unrecognized MCP under its own server cluster.
  const m = id.match(/^([a-z0-9]+(?:-[a-z0-9]+)+)_/)
  if (m) return m[1]
  return "other"
}

/** Human label for any cluster key — named, dynamic MCP server, or "other". */
export function clusterLabel(key: string): string {
  const known = BY_KEY.get(key)
  if (known) return known.label
  if (key === "other") return "Other tools"
  const name = key.replace(/^user-/, "").replace(/[-_]+/g, " ").trim()
  return name ? name.replace(/\b\w/g, (c) => c.toUpperCase()) : key
}

function blurbFor(key: string): string {
  const known = BY_KEY.get(key)
  if (known) return known.blurb
  if (key === "other") return "other installed tools"
  return `${clusterLabel(key)} integration`
}

function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 1)
}

// Discover the deferrable clusters present in the catalog (non-core tools only),
// with the set of description tokens for each — so search works for dynamic
// clusters (e.g. ServiceNow) too, by matching the query against tool descriptions.
function catalogClusters(catalog: CatalogEntry[]): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>()
  for (const entry of catalog) {
    if (isAlwaysOn(entry.id)) continue
    const key = clusterOf(entry.id)
    let set = out.get(key)
    if (!set) {
      set = new Set()
      out.set(key, set)
    }
    for (const t of tokenize(entry.id + " " + entry.description)) set.add(t)
  }
  return out
}

// Match a free-text query to deferrable clusters. Scores each cluster present in
// the catalog by overlap between query tokens and (a) the cluster's keywords (if
// named) and (b) its tools' id+description tokens. Top-scoring keys (ties kept,
// capped at 2); empty when nothing matches.
export function matchClusters(query: string, catalog: CatalogEntry[]): string[] {
  const qtokens = new Set(tokenize(query))
  if (qtokens.size === 0) return []
  const present = catalogClusters(catalog)
  const scored: { key: string; score: number }[] = []
  for (const [key, descTokens] of present) {
    let score = 0
    const known = BY_KEY.get(key)
    if (known)
      for (const kw of known.keywords) {
        const kwTokens = tokenize(kw)
        if (kwTokens.length && kwTokens.every((t) => qtokens.has(t))) score += 2
      }
    for (const t of qtokens) if (descTokens.has(t)) score += 1
    if (score > 0) scored.push({ key, score })
  }
  if (scored.length === 0) return []
  scored.sort((a, b) => b.score - a.score)
  const top = scored[0].score
  return scored
    .filter((s) => s.score === top)
    .slice(0, 2)
    .map((s) => s.key)
}

// Render the deferred-families menu — clusters present in the catalog that aren't
// always-on and haven't been activated yet. Named clusters first (stable order),
// then dynamic (alphabetical). Returns undefined when nothing is deferred.
export function buildMenu(catalog: CatalogEntry[], activated: ReadonlySet<string>): string | undefined {
  const counts = new Map<string, number>()
  for (const entry of catalog) {
    if (isAlwaysOn(entry.id)) continue
    const key = clusterOf(entry.id)
    if (activated.has(key)) continue
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  if (counts.size === 0) return undefined

  const lines: string[] = []
  for (const c of CLUSTERS) {
    const n = counts.get(c.key)
    if (!n) continue
    lines.push(`  • ${c.label} (${n}) — ${c.blurb}`)
    counts.delete(c.key)
  }
  for (const key of [...counts.keys()].sort()) {
    lines.push(`  • ${clusterLabel(key)} (${counts.get(key)}) — ${blurbFor(key)}`)
  }
  return [
    "## Additional tools — load on demand",
    "Only your core tools are loaded now. The capability families below are available but NOT loaded yet.",
    'To use one, call tool_search("<what you need>") and the whole family loads for your next step.',
    "",
    ...lines,
    "",
    "If you're unsure whether a capability exists, search first before saying you can't.",
  ].join("\n")
}

// Format the tools that just loaded, for tool_search's return value.
export function formatLoaded(keys: string[], catalog: CatalogEntry[]): string {
  const set = new Set(keys)
  const lines: string[] = []
  for (const entry of catalog) {
    if (isAlwaysOn(entry.id)) continue
    if (!set.has(clusterOf(entry.id))) continue
    const first = (entry.description ?? "").split("\n")[0].slice(0, 200)
    lines.push(`  • ${entry.id} — ${first}`)
  }
  return [`Loaded ${keys.map(clusterLabel).join(", ")}. You can now call these tools:`, "", ...lines].join("\n")
}

// All deferred families present (shown when a search matched nothing). Uses the
// catalog when given so dynamic families (e.g. ServiceNow) are listed too.
export function familiesList(catalog?: CatalogEntry[]): string {
  if (catalog && catalog.length) {
    const keys = new Set<string>()
    for (const entry of catalog) if (!isAlwaysOn(entry.id)) keys.add(clusterOf(entry.id))
    const named = CLUSTERS.map((c) => c.key).filter((k) => keys.has(k))
    const dynamic = [...keys].filter((k) => !BY_KEY.has(k)).sort()
    return [...named, ...dynamic].map((k) => `  • ${clusterLabel(k)} — ${blurbFor(k)}`).join("\n")
  }
  return CLUSTERS.map((c) => `  • ${c.label} — ${c.blurb}`).join("\n")
}
