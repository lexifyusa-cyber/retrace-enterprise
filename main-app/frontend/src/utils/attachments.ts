/**
 * Attachment helpers — convert browser File objects into the OC-compatible
 * `{type:"file", mime, filename, url:"data:..."}` part shape. OC's web UI
 * stores attachments as inline base64 data URLs in the part JSON (verified
 * against `~/.local/share/opencode/opencode.db`'s `part.data` column); we
 * match that exactly so OC's prompt assembly + storage works without any
 * server-side upload route.
 */

/** OC's part shape for attachments. `url` is an RFC 2397 data URL. */
export interface FilePart {
  type: 'file'
  mime: string
  filename: string
  /** `data:<mime>;base64,<b64>` — what OC stores in `part.data` for image+file attachments. */
  url: string
}

/** 25 MB cap. Bigger than OC's typical use; data URLs add ~33% overhead so
 * the post-encode payload is up to ~33 MB. Aligns with the user's choice in
 * the implementation plan. */
export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

export class AttachmentTooLargeError extends Error {
  constructor(public filename: string, public bytes: number) {
    super(`Attachment "${filename}" is ${(bytes / 1024 / 1024).toFixed(1)} MB (limit: 25 MB)`)
    this.name = 'AttachmentTooLargeError'
  }
}

export function isImageMime(mime: string): boolean {
  return typeof mime === 'string' && mime.toLowerCase().startsWith('image/')
}

/** Read a browser File into the FilePart shape OC expects. Rejects if the
 * file exceeds MAX_ATTACHMENT_BYTES so we don't ship a 30-MB POST body that
 * gets bounced by Vite/uvicorn. */
export function fileToDataUrl(file: File): Promise<FilePart> {
  return new Promise((resolve, reject) => {
    if (file.size > MAX_ATTACHMENT_BYTES) {
      reject(new AttachmentTooLargeError(file.name, file.size))
      return
    }
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error ?? new Error('FileReader error'))
    reader.onload = () => {
      const result = reader.result
      if (typeof result !== 'string') {
        reject(new Error('FileReader did not return a string'))
        return
      }
      // result is already a `data:<mime>;base64,<b64>` data URL. Trust File.type;
      // when blank (rare for clipboard images), fall back to application/octet-stream
      // — OC will render it as a generic file card.
      const mime = file.type || 'application/octet-stream'
      resolve({
        type: 'file',
        mime,
        filename: file.name || 'attachment',
        url: result,
      })
    }
    reader.readAsDataURL(file)
  })
}

/** Extract files from a clipboard paste event. Returns an empty array when
 * the paste was pure text (no file items). */
export function filesFromClipboard(e: ClipboardEvent): File[] {
  const items = e.clipboardData?.items
  if (!items) return []
  const out: File[] = []
  for (let i = 0; i < items.length; i++) {
    const item = items[i]
    if (item.kind === 'file') {
      const f = item.getAsFile()
      if (f) out.push(f)
    }
  }
  return out
}

/** Pretty byte size for the file-card chip. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
