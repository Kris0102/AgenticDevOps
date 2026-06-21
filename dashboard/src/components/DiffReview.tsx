import { Diff, Hunk, parseDiff, type FileData } from 'react-diff-view'
import type { DiffData } from '../api'

// What the human sees at a `code` gate: the raw unified `git diff` rendered green/red.
// diff_text comes straight from artifacts.content_text on the backend.
export function DiffReview({ diff }: { diff: DiffData }) {
  let files: FileData[] = []
  try {
    files = parseDiff(diff.diff_text)
  } catch {
    files = []
  }

  return (
    <div className="diffreview">
      <p className="summary">{diff.summary}</p>

      {files.length === 0 && (
        <pre className="rawdiff">{diff.diff_text || '(empty diff)'}</pre>
      )}

      {files.map((file, i) => (
        <div key={i} className="difffile">
          <div className="fhead">
            {file.oldPath === file.newPath ? file.newPath : `${file.oldPath} → ${file.newPath}`}
          </div>
          <Diff viewType="unified" diffType={file.type} hunks={file.hunks}>
            {(hunks) => hunks.map((hunk) => <Hunk key={hunk.content} hunk={hunk} />)}
          </Diff>
        </div>
      ))}
    </div>
  )
}
