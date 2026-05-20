import type { Permission } from "../permission"
import type { Agent } from "./agent"

/**
 * Build the `permission` ruleset for a subagent's session when it's spawned
 * via the task tool. Combines:
 *
 * 1. The parent **agent's** edit-class deny rules — Plan Mode's file-edit
 *    restriction lives on the agent ruleset, not on the session, so a
 *    subagent that only inherited the parent SESSION's permission would
 *    silently bypass it. (#26514)
 * 2. The parent **session's** EFFECTIVE deny rules and external_directory
 *    rules. Effective = the rule that `findLast` would pick for its
 *    (permission, pattern) tuple. The original code forwarded every deny
 *    in the parent's permission array, but the session permission
 *    accumulates over time (each HTTP PATCH appends via Permission.merge).
 *    A toggle-off-then-on sequence leaves both `bash:*:deny` and a later
 *    `bash:*:allow` in the parent; the parent's `findLast` picks allow, but
 *    the unfiltered inheritance handed the subagent a stale `bash:*:deny`
 *    that nothing overrides on the subagent side, so the subagent was
 *    silently more restrictive than the parent. Dedup per (permission,
 *    pattern) keeping the LAST occurrence, then forward only the rules
 *    that are still denies after that dedup.
 * 3. Default `todowrite` and `task` denies if the subagent's own ruleset
 *    doesn't already permit them.
 */
export function deriveSubagentSessionPermission(input: {
  parentSessionPermission: Permission.Ruleset
  parentAgent: Agent.Info | undefined
  subagent: Agent.Info
}): Permission.Ruleset {
  const canTask = input.subagent.permission.some((rule) => rule.permission === "task")
  const canTodo = input.subagent.permission.some((rule) => rule.permission === "todowrite")
  const parentAgentDenies =
    input.parentAgent?.permission.filter((rule) => rule.action === "deny" && rule.permission === "edit") ?? []
  const lastIndexByKey = new Map<string, number>()
  input.parentSessionPermission.forEach((rule, i) => {
    lastIndexByKey.set(`${rule.permission}|${rule.pattern}`, i)
  })
  const effectiveParentSession = input.parentSessionPermission.filter(
    (rule, i) => lastIndexByKey.get(`${rule.permission}|${rule.pattern}`) === i,
  )
  return [
    ...parentAgentDenies,
    ...effectiveParentSession.filter(
      (rule) => rule.permission === "external_directory" || rule.action === "deny",
    ),
    ...(canTodo ? [] : [{ permission: "todowrite" as const, pattern: "*" as const, action: "deny" as const }]),
    ...(canTask ? [] : [{ permission: "task" as const, pattern: "*" as const, action: "deny" as const }]),
  ]
}
