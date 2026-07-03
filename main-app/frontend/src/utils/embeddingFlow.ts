export type EmbeddingSettingsPayload = Record<string, unknown> & {
  embedding_model?: string | null
  embedding_base_url?: string | null
  embedding_api_key?: string
  embedding_chroma_collection?: string | null
  embedding_debug_log?: boolean
}

export interface EmbeddingSettingsFormState {
  embeddingModel: string
  embeddingBaseUrl: string
  embeddingApiKey: string
  embeddingChromaCollection: string
  embeddingDebugLog: boolean
}

export function embeddingBaseUrlInputValue(configuredBaseUrl?: string | null): string {
  return configuredBaseUrl || ''
}

export function applyEmbeddingSettingsToPayload<T extends EmbeddingSettingsPayload>(
  payload: T,
  state: EmbeddingSettingsFormState,
): T {
  payload.embedding_model = state.embeddingModel.trim() || null
  payload.embedding_base_url = state.embeddingBaseUrl.trim() || null
  if (state.embeddingApiKey.trim()) {
    payload.embedding_api_key = state.embeddingApiKey.trim()
  }
  payload.embedding_chroma_collection = state.embeddingChromaCollection.trim() || null
  payload.embedding_debug_log = state.embeddingDebugLog
  return payload
}

export function buildRetrievalDebugPayload(question: string, topK = 5): { question: string; top_k: number } {
  return {
    question: question.trim(),
    top_k: topK,
  }
}
