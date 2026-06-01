// Per-session, process-local state for the experimental tool-search feature
// (OPENCODE_EXPERIMENTAL_TOOL_SEARCH). Holds, per session: the tool catalog
// (id + description, refreshed each turn by prompt.ts resolveTools) and the set
// of activated clusters (grown by the tool_search tool).
//
// Implemented as a plain module singleton rather than an Effect service: OC runs
// one process per user (RETRACE_OC_USER_ID), and this is process-local
// optimization state, so it needs no layer wiring. Read by llm.ts (activeTools
// filter) and prompt.ts (deferred-families menu + catalog population).

export interface CatalogEntry {
  id: string
  description: string
}

interface SessionState {
  catalog: CatalogEntry[]
  clusters: Set<string>
}

const sessions = new Map<string, SessionState>()

function ensure(sessionID: string): SessionState {
  let state = sessions.get(sessionID)
  if (!state) {
    state = { catalog: [], clusters: new Set() }
    sessions.set(sessionID, state)
  }
  return state
}

/** Replace the catalog for a session (called once per turn from resolveTools). */
export function setCatalog(sessionID: string, catalog: CatalogEntry[]): void {
  ensure(sessionID).catalog = catalog
}

/** All tools known this session (id + full description), for search + menu. */
export function getCatalog(sessionID: string): CatalogEntry[] {
  return ensure(sessionID).catalog
}

/** Mark cluster keys as activated for a session (whole families load on demand). */
export function activate(sessionID: string, clusters: Iterable<string>): void {
  const state = ensure(sessionID)
  for (const cluster of clusters) state.clusters.add(cluster)
}

/** Clusters activated so far this session. */
export function activatedClusters(sessionID: string): ReadonlySet<string> {
  return ensure(sessionID).clusters
}

export * as ToolActivation from "./tool-activation"
