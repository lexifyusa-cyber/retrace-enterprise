"""
RAG (Retrieval-Augmented Generation) pipeline for ReTrace.

Revamped 4-phase training pipeline (no embedding during training):
  Phase 1: Build tree + extension-based exclusion (no LLM)
  Phase 2: LLM project analysis + keep/exclude filtering (rich metadata stored in KB)
  Phase 3: Text extraction — direct read for text, OCR probe + full OCR for mixed
  Phase 4: Build compressed+encrypted KB (with LLM analysis baked in)

The knowledge base stores THREE layers:
  1. Structural knowledge — LLM's project analysis, folder purposes, file descriptions
  2. Content knowledge   — full extracted text per file
  3. Chunk map           — maps chunk IDs to file offsets for retrieval

Embedding is NOT part of training — it happens at retrieval time or
via a separate indexing step if needed later.

Usage:
    from app.rag.pipeline import TrainingPipeline, run_product_training
    from app.rag.kb_store import KnowledgeBaseStore
"""
