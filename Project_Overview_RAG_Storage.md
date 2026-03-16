# KnowledgePod — RAG & Chunk Storage: Project Overview

Detailed overview of the RAG (retrieval-augmented generation) and chunk storage changes implemented: ChromaDB-only index, no chunk text in any database, and read-from-disk at query time (Cursor-style).

---

## 1. Design goals

| Goal | Description |
|------|-------------|
| **Reduce storage** | Stop storing chunk text and embeddings in SQLite; stop storing chunk text (documents) in ChromaDB. |
| **Single source of truth for chunk text** | Chunk content lives only in the **original source files** on disk. At query time we read the relevant lines using path + line range. |
| **ChromaDB as the only chunk index** | All chunk metadata and vectors live in Chroma only. SQLite is no longer used for chunk storage at all. |

---

## 2. What is stored where

### 2.1 ChromaDB (only chunk index)

| Stored | Not stored |
|--------|------------|
| **ids** — chunk UUIDs | **documents** — chunk text (removed) |
| **embeddings** — vectors from the embedding model | |
| **metadatas** — see below | |

**Metadata fields per chunk:**

- `product_id`, `processing_type`, `sub_category`, `source_path`
- `component`, `feature`, `concepts`, `summary_scope`
- `start_line`, `end_line` — 1-based line range in the source file (0 if N/A, e.g. PDF)
- `indexed_at` — timestamp for ordering and “last indexed” per file

### 2.2 SQLite

- **Chunk records:** We **no longer write** to `chunk_records`. The table may still exist for the one-time migration `migrate_sqlite_to_chroma()` (SQLite → Chroma backfill). No new rows are created for new indexing.
- **Everything else** (products, folder_groups, users, conversations, docs, SOPs, settings, etc.) is unchanged and still in SQLite.

### 2.3 Chunk text

- **Not stored** in Chroma or SQLite.
- **Source of truth:** The original files (e.g. `.py`, `.md`, `.txt`) on disk. For each chunk we store only `source_path`, `start_line`, and `end_line` in Chroma. At query time we open the file and read those lines.

---

## 3. File processor: line ranges (start_line / end_line)

Chunks need a way to point back to the exact lines in the source file so we can read the text from disk later.

### 3.1 Helper: `_line_range_for_piece(full_text, piece)`

- **Location:** `main-app/backend/app/rag/file_processor.py`
- **Returns:** `(start_line, end_line)` 1-based inclusive, or `(0, 0)` if the piece is not found in the full text.
- **Use:** Given the full file text and a substring (chunk content), find the piece and compute its line range.

### 3.2 Code chunks

- **`_split_code_into_symbols`** now returns `(symbol_name, block_text, start_line, end_line)` per block (1-based).
- For each sliding-window **piece** inside a block we compute the piece’s line range within the block, then map to file lines: `start_line = block_start_line + piece_start - 1`, same for `end_line`.
- **ChunkMetadata** for code chunks gets `start_line` and `end_line` set (or `None` when N/A).

### 3.3 Doc chunks

- **Plain text** (e.g. `.txt`, `.rst`, `.md` without section split): we have full file text; for each piece from the sliding window we set `start_line` / `end_line` via `_line_range_for_piece(text, piece)`.
- **Markdown with sections:** we have `full_text` and section `body`; we use `_line_range_for_piece(full_text, piece)` so each chunk gets a file line range.
- **PDF, DOCX, HTML:** no file line numbers; `start_line` / `end_line` are left unset (or 0 in Chroma).

### 3.4 Other / fallback

- Same as plain-text doc: full file text + `_line_range_for_piece` for each sliding-window piece.

### 3.5 Ticket export, diagram image, doc_with_diagrams

- No meaningful file line range; `start_line` / `end_line` are `None` or 0.

---

## 4. Embedding index: indexing (no SQLite, no documents in Chroma)

### 4.1 `index_chunks` — what gets written

- **ChromaDB only.** We no longer insert into `chunk_records` in SQLite.
- For each batch we send to Chroma:
  - `ids` — chunk IDs  
  - `embeddings` — from `embed_texts(texts)`  
  - `metadatas` — including `source_path`, `start_line`, `end_line`, `indexed_at` (and the rest).  
- **No `documents`** argument; Chroma does not store chunk text.

### 4.2 Migration: nullable text/embedding in SQLite

- For existing DBs that still have the `chunk_records` table, `app/db/database.py` runs a migration so `text` and `embedding` columns are nullable (recreate table, copy data, drop old, rename). New code does not write to this table; the migration only updates old schemas.

---

## 5. Embedding index: search (text from disk)

### 5.1 Chroma query

- We query Chroma with `include=["metadatas", "distances"]` only (no `documents`).

### 5.2 Helper: `read_file_lines(path, start_line, end_line)`

- **Location:** `main-app/backend/app/rag/embedding_index.py`
- **Behavior:** Opens the file at `path`, reads lines from `start_line` to `end_line` (1-based inclusive), returns that string. Returns `""` on missing file, invalid range, or read error.

### 5.3 Resolving chunk text for each hit

- For each result we have `source_path`, `start_line`, `end_line` from metadata.
- If `path` and `start_line`/`end_line` are valid (`start_line <= end_line`), we set **text = read_file_lines(path, start_line, end_line)**.
- Otherwise (e.g. PDF, legacy data without line range) we set **text = ""**. We no longer fall back to SQLite, since chunk text is not stored there.

### 5.4 Returned chunks

- Each hit is turned into a **Chunk** with `id`, `source_path`, `processing_type`, **text** (from disk or empty), and **metadata** (score, component, etc.). The caller (e.g. knowledge-base tool, RAG API) uses this text to build the LLM context.

---

## 6. List chunks, training tree, agent-qa sync (Chroma only)

All chunk listing and tree data now come from Chroma; no reads from `chunk_records`.

### 6.1 `get_indexed_files(product_id)`

- **Before:** SQL query on `chunk_records` grouped by `source_path`, max `created_at`.
- **After:** Chroma `collection.get(where={"product_id": product_id}, include=["metadatas"])`. We group by `source_path` and take max `indexed_at` per path, then return `{source_path: datetime}`. Used by the pipeline to decide which files need re-processing.

### 6.2 `clear_file_chunks(product_id, source_paths)` / `clear_product(product_id)`

- **Before:** Deleted from SQLite and from Chroma.
- **After:** Delete only from Chroma (by matching `source_path` in metadata, or by `product_id` for clear_product).

### 6.3 List chunks API — `GET /api/v1/products/{product_id}/chunks`

- **Before:** Paginated query on `chunk_records` (order by `created_at`, limit/offset).
- **After:** `EmbeddingIndexService.get_chunks_list(product_id, processing_type?, limit, offset)`. It uses Chroma `get` with optional `processing_type` filter, sorts by `indexed_at` in Python, applies offset/limit. For each chunk, text is loaded via `read_file_lines` when path and line range exist; otherwise text is `""`. Returns the same response shape (chunk_id, source_path, processing_type, text, metadata, created_at, has_embedding).

### 6.4 Training tree API — `GET /api/v1/products/{product_id}/training-tree`

- **Before:** SQL grouping on `chunk_records` by `processing_type` and `source_path`, plus distinct `source_path` for sub_category from metadata.
- **After:** `EmbeddingIndexService.get_training_tree_data(product_id)`. Chroma `get` for the product; in Python we group by `(processing_type, source_path)`, count chunks, and take `sub_category` from metadata. The API builds the same tree structure (type → sub_category → files with chunk counts).

### 6.5 Agent Q&A sync — clearing old agent-qa chunks

- **Before:** SQL select `ChunkRecord.source_path` where `product_id` and `source_path LIKE 'agent-qa://%'`, then `clear_file_chunks(product_id, old_paths)`.
- **After:** `EmbeddingIndexService.get_source_paths_by_prefix(product_id, "agent-qa://")` (Chroma `get` + filter in Python), then `clear_file_chunks(product_id, old_paths)`.

---

## 7. Content types: remove Configuration and Diagrams

- **Add Training Data** (and any content-type dropdown): **Configuration** and **Diagrams** were removed. Only the remaining types (e.g. code, doc, other, ticket_export, etc.) are offered.
- **Backend:** Validation and folder_group normalization treat `configuration` and `diagrams` as legacy; they are mapped to `code` and `documentation` where needed for training context and display.
- **Migration script:** `main-app/backend/migrate_remove_diagrams_config.py` (one-time) to clean any existing rows that had those types; in practice no rows required updating.

---

## 8. End-to-end retrieval flow (summary)

| Step | What happens |
|------|----------------|
| 1 | Caller calls `index.search(product_id, query, top_k, filters)`. |
| 2 | Query string is embedded to a vector. |
| 3 | Chroma **where** filter is built (product_id + optional processing_type, sub_category, etc.). |
| 4 | Chroma **ANN search** returns ids, metadatas, distances (no documents). |
| 5 | For each hit: if metadata has valid path and start_line/end_line → **read_file_lines(path, start_line, end_line)** → chunk text; else text is `""`. |
| 6 | Return list of **Chunk**(id, source_path, processing_type, **text**, metadata with score). |

Chunk text is **never** read from a database; it is read from the **original file on disk** when a valid line range exists.

---

## 9. One-time migration: SQLite → Chroma

- **Function:** `migrate_sqlite_to_chroma()` in `embedding_index.py`.
- **Purpose:** For existing deployments that had embeddings (and optionally text) in `chunk_records`, copy those into Chroma so search works without re-indexing.
- **Behavior:** If Chroma already has data, skip. Otherwise load from SQLite where `embedding IS NOT NULL`, then `collection.add(ids, embeddings, metadatas)` with `start_line`/`end_line` from `metadata_json` and `indexed_at` set. No `documents` are sent to Chroma.
- **New installs:** No chunk rows in SQLite, so nothing to migrate; all indexing goes only to Chroma.

---

## 10. Files modified

| File | Changes |
|------|--------|
| `main-app/backend/app/rag/file_processor.py` | Added `_line_range_for_piece`; `_split_code_into_symbols` returns 4-tuple with line range; code/doc/other chunks get `start_line`/`end_line` in metadata. |
| `main-app/backend/app/rag/embedding_index.py` | No SQLite writes in `index_chunks`; Chroma metadatas include `start_line`, `end_line`, `indexed_at`; no `documents` in Chroma; `read_file_lines`; search uses only disk for text; `get_indexed_files`/`clear_file_chunks`/`clear_product` use Chroma only; added `get_chunks_list`, `get_training_tree_data`, `get_source_paths_by_prefix`; removed `_get_chunk_texts_by_ids`. |
| `main-app/backend/app/rag/models.py` | `ChunkMetadata`: `start_line`/`end_line`; `ChunkRecord.text` and `ChunkRecord.embedding` nullable; `to_chunk()` uses `self.text or ""`. |
| `main-app/backend/app/db/database.py` | Migration to make `chunk_records.text` and `chunk_records.embedding` nullable (recreate table, copy, drop, rename). |
| `main-app/backend/app/api/products.py` | Sync agent Q&A: old paths from `index.get_source_paths_by_prefix`; training tree from `index.get_training_tree_data`; list chunks from `index.get_chunks_list`; no ChunkRecord queries for chunks. |
| `main-app/backend/app/api/groups.py` | (If applicable) content-type validation/dropdown without config/diagrams. |
| `main-app/backend/app/models/folder_group.py` | `to_dict()` normalization for config/diagrams; validation. |
| `main-app/frontend/src/pages/Products.tsx` | Add Training Data content-type dropdown without Configuration/Diagrams. |
| `main-app/frontend/src/pages/FolderGroups.tsx` | Same content-type options. |
| `main-app/backend/migrate_remove_diagrams_config.py` | One-time script to remove or normalize config/diagrams in DB. |

---

## 11. Notes

- **Path for read_file_lines:** `source_path` is used as the file path as-is. If your pipeline uses relative paths, they must be resolvable from the process working directory (or you may need a base path in the future).
- **Chunks without line range:** PDF, DOCX, agent-qa, diagram, and similar chunks have no file line range. For those, search still returns the chunk (id, path, type, score) but **text** will be empty unless you add another source (e.g. store a short snippet in metadata or a separate store).
- **ChunkRecord table:** Still created by the ORM and may contain old rows. No new chunk rows are written; the table is only used for the one-time `migrate_sqlite_to_chroma()` if you run it.
- **Embeddings:** Stored only in Chroma. They cannot be “converted back” to text; they are used only for similarity search. The text sent to the LLM is always the content read from disk (or empty).

---

## 12. Recent implementations (agent, models, build)

Summary of changes implemented in the same period as the RAG work (agent fixes for Google/Sarvam, repo hygiene, Windows portable build).

### 12.1 ScreenOps + Google/Gemini models

- **Problem:** ScreenOps vision calls failed for Google Gemini (e.g. 400, content-format errors).
- **Solution:** In `main-app/backend/app/tools/screenops/tool.py`:
  - **`_is_google_model(model)`** — Detects Google/Gemini via `api_url` (`generativelanguage.googleapis.com`, `aiplatform.googleapis.com`) or model name starting with `gemini`.
  - **`_make_google_vision_invoker(model)`** — Calls Gemini's OpenAI-compatible endpoint with strictly typed content: `{"type": "text", "text": "..."}` and `{"type": "image_url", "image_url": {...}}` (no bare strings).
  - **`build_screenops_tool()`** — Uses x.ai invoker for x.ai, **Google invoker for Google/Gemini**, else generic LangChain invoker. Generic invoker also uses typed text parts.
- ScreenOps vision now works for Google models without changing behavior for other providers.

### 12.2 CodeAct agent: Gemini-only (thought_signature) fix

- **Problem:** With Gemini 2.5 (thinking models), the second API call in a turn failed with 400: *"Function call is missing a thought_signature in functionCall parts"* — synthetic `code_execution` tool_calls had no `thought_signature`.
- **Solution:** In `main-app/backend/app/services/agent_service.py`:
  - **`_is_gemini_model(llm_settings)`** — Same detection as ScreenOps (api_url or model name `gemini`).
  - **`_build_codeact_graph(..., use_gemini_compat)`** — When `use_gemini_compat` is True:
    - **call_model:** Do **not** add synthetic `tool_calls` to the model response when code is extracted.
    - **sandbox:** Append a **`HumanMessage`** with `"Code execution result:\n\n" + output` instead of a `ToolMessage`.
  - Conversation sent to Gemini is strictly **user → assistant → user** (no function_call parts).
- **Scope:** Applied only when the chat model is detected as Gemini; OpenAI/Anthropic/etc. unchanged.

### 12.3 CodeAct agent: Sarvam model support

- **Problem:** Sarvam's API returned 400: *"User and assistant turns must alternate, starting with a user message!"* — it does not accept tool-turn format.
- **Solution:** Same approach as Gemini:
  - **`_is_sarvam_model(llm_settings)`** — True when `api_url` contains `sarvam.ai` or `model_name` contains `sarvam` (case-insensitive).
  - **`use_gemini_compat = _is_gemini_model(llm_settings) || _is_sarvam_model(llm_settings)`** — Sarvam uses the same alternating-messages path (no synthetic tool_calls, code result as user message).
- **Files:** `agent_service.py` (detection + same graph compat path).

### 12.4 Repo hygiene

- **.gitignore:** Added entries so generated file-dump artifacts are not committed:
  - `main-app/backend/product_files.txt`
  - `main-app/backend/product_files_content.txt`
  - `main-app/backend/product_files_for_desktop.txt`
- These are large, auto-generated lists (e.g. from ScreenOps/debug) and should remain local.

### 12.5 Windows portable EXE (no-install build)

- **Goal:** Produce a single Windows `.exe` that runs from wherever it is placed (no installer).
- **Changes:**
  - **`main-app/frontend/package.json`** — `win.target` changed from `["nsis", "zip"]` to **`[{ "target": "portable", "arch": ["x64"] }]`** so electron-builder outputs a portable exe.
  - **`main-app/frontend/electron/main.js`** — Windows support:
    - **`getAppSupportDir()`** — On Windows uses `%APPDATA%\KnowledgePod` (on Mac: `~/Library/Application Support/KnowledgePod`).
    - **`startBackend()`** — On Windows uses `venv\Scripts\python.exe`; on Mac/Linux uses `venv/bin/python`.
  - **`scripts/build-portable-win.ps1`** — PowerShell script (run on Windows): build React frontend, bundle Python backend with Windows venv, copy frontend dist into bundle, run `electron-builder --win`. Output: `main-app/frontend/dist-electron/*.exe`.
  - **`scripts/build-portable-win.bat`** — Batch wrapper that invokes the PowerShell script.
- **Note:** The portable exe must be **built on Windows** (Python venv and Electron packaging are platform-specific). Copy the resulting exe to any folder and run it; app data is stored under `%APPDATA%\KnowledgePod`.

### 12.6 Files touched (this week's agent/build work)

| Area | File |
|------|------|
| ScreenOps + Google | `main-app/backend/app/tools/screenops/tool.py` |
| Agent Gemini/Sarvam | `main-app/backend/app/services/agent_service.py` |
| Repo hygiene | `.gitignore` |
| Windows portable | `main-app/frontend/package.json`, `main-app/frontend/electron/main.js`, `scripts/build-portable-win.ps1`, `scripts/build-portable-win.bat` |
