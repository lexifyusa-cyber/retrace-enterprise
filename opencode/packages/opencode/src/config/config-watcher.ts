/**
 * Config-file watcher.
 *
 * Forks at server start; watches OPENCODE_CONFIG_DIR for opencode.json
 * changes; tears down every cached instance via POST /instance/dispose-all
 * so the next request reads the new config and reconnects MCP servers fresh.
 *
 * Why: ReTrace's `generate_config` writes opencode.json on every model /
 * team / tool / settings / MCP change. Before this watcher, only some of
 * ReTrace's code paths followed up with `POST /instance/dispose`, so those
 * other changes died on disk. The watcher moves the reload contract into
 * OpenCode — ReTrace's job becomes "write the file."
 *
 * Why HTTP (not in-process `store.disposeAll()`): the HTTP server's
 * `listenerLayer` is built with a fresh `Layer.makeMemoMapUnsafe()`
 * (see `server/server.ts:127-141`), so its InstanceStore has its own cache.
 * The watcher's app-runtime InstanceStore has a different (empty) cache,
 * so calling `disposeAll()` on it silently does nothing. Going through
 * HTTP means the request handler captures the right (HTTP server's) store.
 *
 * Self-gates on OPENCODE_CONFIG_DIR — does nothing in plain `opencode tui`
 * / `opencode run` use where the env var isn't set.
 *
 * Debounces ~400 ms (coalesces partial writes), skips no-op rewrites via
 * SHA1 content compare (ReTrace re-writes the file on every page load even
 * when bytes are identical), and waits for any busy session to go idle
 * before disposing — a streaming agent turn is never interrupted. Busy/idle
 * is tracked via GlobalBus, which mirrors every per-instance bus publish
 * (see `bus/index.ts:101`).
 */
import { Cause, Effect } from "effect"
// @ts-ignore
import { createWrapper } from "@parcel/watcher/wrapper"
import type ParcelWatcher from "@parcel/watcher"
import { readFile } from "fs/promises"
import { createHash } from "crypto"
import path from "path"
import { GlobalBus } from "@/bus/global"
import { lazy } from "@/util/lazy"
import * as Log from "@opencode-ai/core/util/log"

declare const OPENCODE_LIBC: string | undefined

const log = Log.create({ service: "config.watcher" })
const SUBSCRIBE_TIMEOUT_MS = 10_000
const DEBOUNCE_MS = 400
const CONFIG_FILENAMES = new Set(["opencode.json", "opencode.jsonc"])

const watcher = lazy((): typeof import("@parcel/watcher") | undefined => {
  try {
    const binding = require(
      `@parcel/watcher-${process.platform}-${process.arch}${process.platform === "linux" ? `-${OPENCODE_LIBC || "glibc"}` : ""}`,
    )
    return createWrapper(binding) as typeof import("@parcel/watcher")
  } catch (error) {
    log.error("failed to load watcher binding", { error })
    return
  }
})

function getBackend() {
  if (process.platform === "win32") return "windows"
  if (process.platform === "darwin") return "fs-events"
  if (process.platform === "linux") return "inotify"
}

async function hashConfig(dir: string): Promise<string | null> {
  for (const name of CONFIG_FILENAMES) {
    try {
      const buf = await readFile(path.join(dir, name))
      return createHash("sha1").update(buf).digest("hex")
    } catch {
      // Try next candidate; the file may not exist yet.
    }
  }
  return null
}

export const start = Effect.fn("ConfigWatcher.start")(function* (port: number) {
  const dir = process.env.OPENCODE_CONFIG_DIR
  if (!dir) return

  const w = watcher()
  if (!w) {
    log.warn("native watcher binding unavailable; config auto-reload disabled")
    return
  }
  const backend = getBackend()
  if (!backend) {
    log.warn("watcher backend not supported", { platform: process.platform })
    return
  }

  // The dispose-all endpoint lives in the InstanceContext group, so it
  // requires a `directory` query param. We pass the OC process cwd — which
  // is what WorkspaceRoutingMiddleware would default to anyway. The chosen
  // directory doesn't matter functionally: the handler calls disposeAll()
  // which iterates *every* entry in the (HTTP server's) cache.
  const disposeAllUrl =
    `http://127.0.0.1:${port}/instance/dispose-all` +
    `?directory=${encodeURIComponent(process.cwd())}`

  log.info("init", { dir, disposeAllUrl })

  // Closure state shared between GlobalBus listener and parcel callback.
  // JS is single-threaded so no synchronization needed.
  let lastHash: string | null = yield* Effect.promise(() => hashConfig(dir))
  let debounceTimer: ReturnType<typeof setTimeout> | null = null
  let reloadPending = false
  const runningSessions = new Set<string>()

  const fireDispose = () => {
    fetch(disposeAllUrl, { method: "POST" })
      .then((resp) => {
        if (!resp.ok) {
          log.error("disposeAll HTTP non-200", { status: resp.status })
        }
      })
      .catch((err: unknown) => log.error("disposeAll HTTP failed", { error: String(err) }))
  }

  // GlobalBus listener — tracks which sessions are currently busy across
  // all instances. No cleanup finalizer: when the OC process exits, the
  // EventEmitter listener and the parcel watcher both get torn down.
  const onGlobal = (evt: { payload?: any }) => {
    const p = evt?.payload
    if (!p || p.type !== "session.status") return
    const sessionID: string | undefined = p.properties?.sessionID
    const statusType: string | undefined = p.properties?.status?.type
    if (!sessionID || !statusType) return
    if (statusType === "idle") {
      runningSessions.delete(sessionID)
      if (reloadPending && runningSessions.size === 0) {
        reloadPending = false
        log.info("deferred reload firing — all sessions idle")
        fireDispose()
      }
    } else {
      runningSessions.add(sessionID)
    }
  }
  GlobalBus.on("event", onGlobal)

  const onChangeDebounced = () => {
    void (async () => {
      const next = await hashConfig(dir)
      if (next === null) return
      if (next === lastHash) return
      lastHash = next
      if (runningSessions.size > 0) {
        log.info("config changed; deferring reload — sessions busy", {
          busy: runningSessions.size,
        })
        reloadPending = true
        return
      }
      log.info("config changed; reloading all instances")
      fireDispose()
    })().catch((err) =>
      log.error("reload pipeline failed", { error: String(err) }),
    )
  }

  const cb: ParcelWatcher.SubscribeCallback = (err, evts) => {
    if (err) return
    for (const evt of evts) {
      if (!CONFIG_FILENAMES.has(path.basename(evt.path))) continue
      if (debounceTimer) clearTimeout(debounceTimer)
      debounceTimer = setTimeout(onChangeDebounced, DEBOUNCE_MS)
      return
    }
  }

  const pending = w.subscribe(dir, cb, { backend })
  yield* Effect.gen(function* () {
    yield* Effect.promise(() => pending)
    log.info("subscribed", { dir })
  }).pipe(
    Effect.timeout(SUBSCRIBE_TIMEOUT_MS),
    Effect.catchCause((cause) => {
      log.error("failed to subscribe", { dir, cause: Cause.pretty(cause) })
      pending.then((s) => s.unsubscribe()).catch(() => {})
      return Effect.void
    }),
  )
})

export * as ConfigWatcher from "./config-watcher"
