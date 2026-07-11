import { useRef, useState, type ReactNode } from 'react'

// Two side-by-side panels with a draggable divider between them (horizontal resize) — the
// right panel's width is user-controlled, the left fills the rest. Each panel is ALSO
// vertically resizable via its native bottom-edge handle (see `.rcols .panel` in styles.css).
// Used for the Activity split (live logs | history) so the user can stretch/shrink either box.
export function ResizableCols({
  left, right, initialRight = 360, minRight = 240, minLeft = 320,
}: {
  left: ReactNode
  right: ReactNode
  initialRight?: number
  minRight?: number
  minLeft?: number
}) {
  const [rightW, setRightW] = useState(initialRight)
  const ref = useRef<HTMLDivElement>(null)

  const onDown = (e: React.MouseEvent) => {
    e.preventDefault()
    const onMove = (ev: MouseEvent) => {
      const el = ref.current
      if (!el) return
      const rect = el.getBoundingClientRect()
      const max = rect.width - minLeft
      setRightW(Math.max(minRight, Math.min(rect.right - ev.clientX, max)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'   // no text selection while dragging
  }

  return (
    <div className="rcols" ref={ref}>
      <div className="rcols-left">{left}</div>
      <div className="rcols-handle" onMouseDown={onDown} role="separator"
        aria-orientation="vertical" title="Drag to resize" />
      <div className="rcols-right" style={{ width: rightW }}>{right}</div>
    </div>
  )
}
