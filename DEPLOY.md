# ReTrace — Fly.io deployment pipeline

The FastAPI backend (`main-app/backend`) and the built React frontend
(`main-app/frontend`) are packaged into **one** image and served on a single
origin. The Electron desktop shell and the Go POD agents are **not** part of
this deployment.

- **App:** `retrace-dev`  ->  https://retrace-dev.fly.dev
- **Org:** lumena tech (`personal`)  |  **Region:** `iad`
- **DB:** SQLite on the `retrace_data` Fly volume, mounted at `/data`
  (`DATABASE_URL=sqlite+aiosqlite:////data/retrace.db`).

## How the pipeline works

| Trigger | Workflow | Effect |
|---|---|---|
| Push / merge to `main` | `Deploy main to Fly` | Auto-builds & deploys `main` to the app. |
| Manual (Actions UI) | `Deploy a branch to Fly (manual test)` | Deploys the branch you pick to the **same** app for live testing. |
| Manual (Actions UI) | `Deploy main to Fly` (Run workflow) | Restores the app to `main` after testing. |
| Pull request to `main` | `Docker build check` | Builds the image to catch breakage before merge (no deploy). |

The two deploy workflows share a concurrency group, so they never run at the
same time. Note this is a **single shared environment**: only one branch can be
live at a time, the auto-deploy on a `main` push will overwrite a branch you're
testing, and all deploys share the same SQLite data on the volume.

## One-time setup (required before the pipeline can deploy)

1. **Create a Fly deploy token** (run locally, you must be logged in to Fly):
   ```bash
   fly tokens create deploy -a retrace-dev
   ```
   Copy the full token it prints (starts with `FlyV1 ...`).

2. **Add it as a GitHub Actions secret** named `FLY_API_TOKEN`:
   - GitHub repo -> Settings -> Secrets and variables -> Actions -> New
     repository secret -> Name `FLY_API_TOKEN`, value = the token.
   - Or via CLI: `gh secret set FLY_API_TOKEN -a actions --body "FlyV1 ..."`

3. **Set the LLM / app secrets on Fly** (staged until the next deploy):
   ```bash
   fly secrets set -a retrace-dev \
     OPENAI_API_KEY=sk-... \
     ANTHROPIC_API_KEY=sk-ant-...
   ```
   `JWT_SECRET` is already set. Add any others your config needs
   (see `main-app/backend/.env.example`).

After the PR adding these files is merged to `main`, the first auto-deploy runs.

## Manual deploy from your machine (optional)

```bash
cd <repo root>
fly deploy --remote-only -a retrace-dev   # builds on Fly's remote builder
fly logs -a retrace-dev                   # watch boot / debug
```

## Notes / known headless limitations

The backend pulls in features that don't fully work on a headless Fly VM and
are expected to degrade gracefully:

- **GUI automation** (`pyautogui` / screen-ops) — runs under `xvfb` so imports
  succeed, but there is no real display.
- **Browser automation** (`playwright`) — the package is installed but browser
  binaries are not (`playwright install` is intentionally skipped).
- **Code sandbox** (`llm-sandbox[podman]`) — no Podman on the VM, so the
  sandbox reports unavailable.
- **OpenCode sidecar** (`/opencode` -> `localhost:4096`) — not run here.

If any of these need to work in the hosted environment, that's a follow-up
(extra buildpacks / a separate machine / process group).
