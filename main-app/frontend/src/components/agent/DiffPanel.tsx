import { GitCompare, FilePlus, FileMinus, FileEdit } from 'lucide-react'

export default function DiffPanel({ diff }: { diff: Record<string, string> | null }) {
  if (!diff || Object.keys(diff).length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-rt-text-muted/40 text-sm">
        <GitCompare className="w-5 h-5 mr-2" />
        No file changes in this session
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto p-4 space-y-3">
      <div className="flex items-center gap-2 mb-3">
        <GitCompare className="w-4 h-4 text-rt-primary" />
        <span className="text-sm font-semibold">File Changes</span>
        <span className="text-xs text-rt-text-muted">({Object.keys(diff).length} files)</span>
      </div>
      {Object.entries(diff).map(([file, content]) => {
        const lines = content.split('\n')
        const additions = lines.filter(l => l.startsWith('+') && !l.startsWith('+++')).length
        const deletions = lines.filter(l => l.startsWith('-') && !l.startsWith('---')).length
        return (
          <div key={file} className="rounded-lg border border-rt-border/40 overflow-hidden">
            <div className="flex items-center gap-2 px-3 py-1.5 bg-rt-bg-light border-b border-rt-border/30">
              {additions > 0 && deletions > 0 ? (
                <FileEdit className="w-3.5 h-3.5 text-amber-400" />
              ) : additions > 0 ? (
                <FilePlus className="w-3.5 h-3.5 text-green-400" />
              ) : (
                <FileMinus className="w-3.5 h-3.5 text-red-400" />
              )}
              <span className="text-xs font-mono text-rt-text truncate flex-1">{file}</span>
              <span className="text-[10px] text-green-400">+{additions}</span>
              <span className="text-[10px] text-red-400">-{deletions}</span>
            </div>
            <pre className="text-xs font-mono p-2 max-h-[300px] overflow-auto whitespace-pre-wrap leading-relaxed">
              {lines.map((line, i) => (
                <div key={i} className={
                  line.startsWith('+') && !line.startsWith('+++') ? 'text-green-400 bg-green-500/5' :
                  line.startsWith('-') && !line.startsWith('---') ? 'text-red-400 bg-red-500/5' :
                  line.startsWith('@@') ? 'text-blue-400' :
                  'text-rt-text-muted'
                }>
                  {line}
                </div>
              ))}
            </pre>
          </div>
        )
      })}
    </div>
  )
}
