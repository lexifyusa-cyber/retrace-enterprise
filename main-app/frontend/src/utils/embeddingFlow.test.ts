import { describe, expect, it } from 'vitest'

import {
  applyEmbeddingSettingsToPayload,
  buildRetrievalDebugPayload,
  embeddingBaseUrlInputValue,
} from './embeddingFlow'

describe('embedding settings flow', () => {
  it('does not silently default the embedding base URL to a developer host', () => {
    expect(embeddingBaseUrlInputValue(null)).toBe('')
    expect(embeddingBaseUrlInputValue(undefined)).toBe('')
    expect(embeddingBaseUrlInputValue('http://10.0.0.51:8024/v1')).toBe('http://10.0.0.51:8024/v1')
  })

  it('trims and persists explicit embedding settings only', () => {
    const payload = applyEmbeddingSettingsToPayload({}, {
      embeddingModel: ' qwen3-embedding-4b ',
      embeddingBaseUrl: ' http://10.0.0.51:8024/v1/ ',
      embeddingApiKey: ' local-vllm ',
      embeddingChromaCollection: ' teamknowparse_kb_chunks ',
      embeddingDebugLog: true,
    })

    expect(payload).toEqual({
      embedding_model: 'qwen3-embedding-4b',
      embedding_base_url: 'http://10.0.0.51:8024/v1/',
      embedding_api_key: 'local-vllm',
      embedding_chroma_collection: 'teamknowparse_kb_chunks',
      embedding_debug_log: true,
    })
  })

  it('does not send an empty embedding API key that would clear or overwrite the saved key', () => {
    const payload = applyEmbeddingSettingsToPayload({}, {
      embeddingModel: '',
      embeddingBaseUrl: '',
      embeddingApiKey: '   ',
      embeddingChromaCollection: '',
      embeddingDebugLog: false,
    })

    expect(payload).toEqual({
      embedding_model: null,
      embedding_base_url: null,
      embedding_chroma_collection: null,
      embedding_debug_log: false,
    })
    expect(payload).not.toHaveProperty('embedding_api_key')
  })
})

describe('Agent retrieval debug flow', () => {
  it('sends only the hybrid retrieval debug API shape expected by the backend', () => {
    const payload = buildRetrievalDebugPayload('  camera night mode  ', 5)

    expect(payload).toEqual({ question: 'camera night mode', top_k: 5 })
    expect(payload).not.toHaveProperty('mode')
    expect(payload).not.toHaveProperty('retrieval_mode')
  })
})
