# TeamKnowParse

TeamKnowParse is an isolated local document-to-JSON pipeline for team knowledge bases.

It uses a page-level streaming architecture:

```text
upload folder/files
-> page inventory
-> cheap page classification
-> parallel routed extraction
-> clean/validate page output
-> final file JSON
-> file_description
-> simple folder JSON
```

The final file JSON follows the user-facing schema only:

```json
{
  "filename": "example_manual.pdf",
  "file_path": "documents/example_manual.pdf",
  "page_count": 3,
  "tags": ["policy", "procedure", "safety"],
  "pages": [
    {
      "page_number": 1,
      "page_type": "DIGITAL_PURE_TEXT",
      "elements": [],
      "rag_text": "..."
    }
  ]
}
```

Internal audit details such as route, confidence, retry count, warnings, and timing are written separately under each run.

## Default Runtime Config

```env
DIGITAL_WORKERS=32
VLM_WORKERS=8
VLM_BATCH_SIZE=4
PARALLEL_BATCHING_ONLY=true
DIGITAL_QUEUE_MAX_SIZE=128
VLM_QUEUE_MAX_SIZE=32
MAX_INFLIGHT_VLM_BATCHES=2
MAX_RETRIES=1
RENDER_ZOOM=1.5
VLM_ENDPOINT=http://10.0.0.51:8017
FILE_DESC_MIN_PAGE_RAG_CHARS=300
FILE_DESC_MAX_USEFUL_PAGES=10
```

`VLM_WORKERS=8` and `VLM_BATCH_SIZE=4` are configurable defaults, but GPU pressure is controlled by bounded queues and `MAX_INFLIGHT_VLM_BATCHES`.

## Run

```bash
cd /home/ubuntu/Desktop/retrace-enterprise-feature/teamknowparse
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py --server.address 0.0.0.0 --server.port 8503
```

CLI:

```bash
python -m teamknowparse.cli /path/to/folder --mode mixed --out runs
```

## Output

```text
runs/<run_id>/
  folder.json
  files/
    <file>.json
  pages/
    <file>_page_001.final.json
    <file>_page_001.audit.json
  chunks.jsonl
  run_log.json
```

