import { Effect } from "effect"
import { Server } from "../../server/server"
import { effectCmd } from "../effect-cmd"
import { withNetworkOptions, resolveNetworkOptions } from "../network"
import { Flag } from "@opencode-ai/core/flag/flag"
import { ConfigWatcher } from "@/config/config-watcher"

export const ServeCommand = effectCmd({
  command: "serve",
  builder: (yargs) => withNetworkOptions(yargs),
  describe: "starts a headless opencode server",
  // Server loads instances per-request via x-opencode-directory header — no
  // need for an ambient project InstanceContext at startup.
  instance: false,
  handler: Effect.fn("Cli.serve")(function* (args) {
    if (!Flag.OPENCODE_SERVER_PASSWORD) {
      console.log("Warning: OPENCODE_SERVER_PASSWORD is not set; server is unsecured.")
    }
    const opts = yield* resolveNetworkOptions(args)
    const server = yield* Effect.promise(() => Server.listen(opts))
    console.log(`opencode server listening on http://${server.hostname}:${server.port}`)

    // No-op unless OPENCODE_CONFIG_DIR is set. Registers JS-side callbacks
    // (parcel watcher + GlobalBus listener) that run on the event loop —
    // no fiber to keep alive, so a plain yield is enough. Takes the server
    // port so it can POST /instance/dispose-all over loopback (the in-process
    // call would hit the wrong InstanceStore — see config-watcher.ts header).
    yield* ConfigWatcher.start(server.port)

    yield* Effect.never
  }),
})
