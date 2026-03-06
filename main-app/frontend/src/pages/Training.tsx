import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Database, Package, FileCode, FileText, Image, TicketIcon, Search, ChevronDown, ChevronUp, Filter } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { productApi } from '@/utils/api'
import { Product } from '@/types'

interface Chunk {
  chunk_id: string
  source_path: string
  processing_type: string
  text: string
  metadata: Record<string, any>
  created_at: string | null
}

interface ChunksResponse {
  product_id: string
  product_name: string
  total: number
  limit: number
  offset: number
  chunks: Chunk[]
}

export default function Training() {
  const [searchQuery, setSearchQuery] = useState('')
  const [selectedType, setSelectedType] = useState<string>('all')
  const [expandedProducts, setExpandedProducts] = useState<Set<string>>(new Set())

  const { data: products = [], isLoading: productsLoading } = useQuery({
    queryKey: ['products'],
    queryFn: () => productApi.list(),
  })

  // Only show trained products
  const trainedProducts = products.filter(p =>
    p.folder_groups.some(g => g.training_status === 'completed')
  )

  const toggleProduct = (productId: string) => {
    setExpandedProducts(prev => {
      const next = new Set(prev)
      if (next.has(productId)) {
        next.delete(productId)
      } else {
        next.add(productId)
      }
      return next
    })
  }

  const processingTypes = ['all', 'code', 'doc', 'ticket_export', 'diagram_image', 'doc_with_diagrams', 'other']

  return (
    <div className="p-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-3xl font-display font-bold mb-2">Training Data</h1>
        <p className="text-rt-text-muted">
          Browse all indexed knowledge datasets by product
        </p>
      </div>

      {/* Filters */}
      <div className="mb-6 flex gap-4 items-center">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-rt-text-muted" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search datasets by text or file path..."
            className="input pl-10"
          />
        </div>

        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-rt-text-muted" />
          <select
            value={selectedType}
            onChange={(e) => setSelectedType(e.target.value)}
            className="input"
          >
            {processingTypes.map(type => (
              <option key={type} value={type}>
                {type === 'all' ? 'All Types' : type.replace('_', ' ')}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Products List */}
      {productsLoading ? (
        <div className="space-y-4">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="card animate-pulse">
              <div className="h-6 bg-rt-surface rounded w-48 mb-4" />
              <div className="h-4 bg-rt-surface rounded w-full" />
            </div>
          ))}
        </div>
      ) : trainedProducts.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="card text-center py-16"
        >
          <Database className="w-16 h-16 mx-auto text-rt-text-muted mb-4" />
          <h3 className="text-xl font-display font-semibold mb-2">No Trained Products</h3>
          <p className="text-rt-text-muted mb-6 max-w-md mx-auto">
            Train a product first to see indexed datasets here.
          </p>
        </motion.div>
      ) : (
        <div className="space-y-4">
          {trainedProducts.map((product) => {
            const stats = product.folder_groups.find(g => g.metadata?.chunks_indexed)?.metadata as Record<string, any> | undefined
            const chunksIndexed = stats?.chunks_indexed || 0
            const isExpanded = expandedProducts.has(product.product_id)

            return (
              <ProductChunksCard
                key={product.product_id}
                product={product}
                chunksIndexed={chunksIndexed}
                isExpanded={isExpanded}
                onToggle={() => toggleProduct(product.product_id)}
                searchQuery={searchQuery}
                selectedType={selectedType}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

function ProductChunksCard({
  product,
  chunksIndexed,
  isExpanded,
  onToggle,
  searchQuery,
  selectedType,
}: {
  product: Product
  chunksIndexed: number
  isExpanded: boolean
  onToggle: () => void
  searchQuery: string
  selectedType: string
}) {
  const { data: chunksData, isLoading } = useQuery<ChunksResponse>({
    queryKey: ['product-chunks', product.product_id, selectedType],
    queryFn: async () => {
      const params: Record<string, string> = { limit: '100', offset: '0' }
      if (selectedType !== 'all') {
        params.processing_type = selectedType
      }
      const response = await fetch(
        `/api/v1/products/${product.product_id}/chunks?${new URLSearchParams(params)}`
      )
      if (!response.ok) throw new Error('Failed to load datasets')
      return response.json()
    },
    enabled: isExpanded,
  })

  const chunks = chunksData?.chunks || []
  const filteredChunks = chunks.filter(chunk => {
    if (searchQuery) {
      const query = searchQuery.toLowerCase()
      const matchesText = chunk.text.toLowerCase().includes(query)
      const matchesPath = chunk.source_path.toLowerCase().includes(query)
      if (!matchesText && !matchesPath) return false
    }
    return true
  })

  const breakdown = product.folder_groups.find(g => g.metadata?.classification_breakdown)?.metadata?.classification_breakdown as Record<string, number> | undefined

  return (
    <div className="card">
      {/* Product Header */}
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between text-left"
      >
        <div className="flex items-center gap-4 flex-1">
          <div className="w-10 h-10 rounded-lg bg-rt-primary/10 flex items-center justify-center">
            <Package className="w-5 h-5 text-rt-primary" />
          </div>
          <div className="flex-1">
            <h2 className="text-lg font-display font-semibold">{product.product_name}</h2>
            <div className="flex items-center gap-4 mt-1 text-sm text-rt-text-muted">
              <span>{chunksIndexed.toLocaleString()} datasets indexed</span>
              {breakdown && (
                <span className="flex items-center gap-2">
                  {Object.entries(breakdown).map(([type, count]) => (
                    <span key={type} className="badge badge-info text-xs">
                      {type}: {(count as number).toLocaleString()}
                    </span>
                  ))}
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm text-rt-text-muted">
            {isExpanded ? 'Hide' : 'Show'} datasets
          </span>
          {isExpanded ? (
            <ChevronUp className="w-5 h-5 text-rt-text-muted" />
          ) : (
            <ChevronDown className="w-5 h-5 text-rt-text-muted" />
          )}
        </div>
      </button>

      {/* Datasets list */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-6 border-t border-rt-border pt-6"
          >
            {isLoading ? (
              <div className="text-center py-8 text-rt-text-muted">
                Loading datasets...
              </div>
            ) : filteredChunks.length === 0 ? (
              <div className="text-center py-8 text-rt-text-muted">
                {searchQuery ? 'No datasets match your search' : 'No datasets found'}
              </div>
            ) : (
              <>
                <div className="mb-4 flex items-center justify-between">
                  <span className="text-sm text-rt-text-muted">
                    Showing {filteredChunks.length} of {chunksData?.total || 0} datasets
                  </span>
                </div>
                <div className="space-y-3 max-h-[600px] overflow-y-auto">
                  {filteredChunks.map((chunk) => (
                    <ChunkCard key={chunk.chunk_id} chunk={chunk} />
                  ))}
                </div>
              </>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function ChunkCard({ chunk }: { chunk: Chunk }) {
  const [isExpanded, setIsExpanded] = useState(false)

  const typeIcons: Record<string, typeof FileCode> = {
    code: FileCode,
    doc: FileText,
    doc_with_diagrams: FileText,
    ticket_export: TicketIcon,
    diagram_image: Image,
  }

  const Icon = typeIcons[chunk.processing_type] || FileText

  const typeColors: Record<string, string> = {
    code: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    doc: 'bg-green-500/10 text-green-400 border-green-500/20',
    doc_with_diagrams: 'bg-green-500/10 text-green-400 border-green-500/20',
    ticket_export: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
    diagram_image: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
    other: 'bg-gray-500/10 text-rt-text-muted border-gray-500/20',
  }

  return (
    <div className={`p-4 rounded-lg border ${typeColors[chunk.processing_type] || typeColors.other}`}>
      <div className="flex items-start gap-3">
        <div className="p-2 rounded-lg bg-rt-bg flex-shrink-0">
          <Icon className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <span className="badge badge-info text-xs">{chunk.processing_type}</span>
            {chunk.metadata?.language && (
              <span className="badge text-xs">{chunk.metadata.language}</span>
            )}
            {chunk.metadata?.symbol_name && (
              <span className="text-xs text-rt-text-muted">Symbol: {chunk.metadata.symbol_name}</span>
            )}
          </div>

          <p className="text-xs font-mono text-rt-text-muted truncate mb-2" title={chunk.source_path}>
            {chunk.source_path}
          </p>

          <div className="text-sm text-rt-text">
            {isExpanded ? (
              <div className="whitespace-pre-wrap break-words">{chunk.text}</div>
            ) : (
              <div className="line-clamp-3">{chunk.text}</div>
            )}
          </div>

          {chunk.text.length > 200 && (
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              className="text-xs text-rt-primary hover:underline mt-2"
            >
              {isExpanded ? 'Show less' : 'Show more'}
            </button>
          )}

          {chunk.metadata && Object.keys(chunk.metadata).length > 0 && (
            <details className="mt-2">
              <summary className="text-xs text-rt-text-muted cursor-pointer hover:text-rt-text">
                Metadata
              </summary>
              <pre className="mt-1 text-xs bg-rt-surface text-rt-text p-2 rounded overflow-x-auto font-mono">
                {JSON.stringify(chunk.metadata, null, 2)}
              </pre>
            </details>
          )}

          {chunk.created_at && (
            <p className="text-xs text-rt-text-muted mt-2">
              Indexed: {new Date(chunk.created_at).toLocaleString()}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
