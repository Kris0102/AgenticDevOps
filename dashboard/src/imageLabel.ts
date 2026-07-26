// Display helpers for sandbox image names (ADR 0039). Shared by the repo registry (which
// image a repo is configured with) and the run views (which image a run actually used), so
// the same image reads identically in both places.

// The default image, mirrored from SandboxSpec.image in orchestrator/state.py. A run recorded
// with this value used the generic sandbox — worth saying plainly rather than showing a name
// that looks like a deliberate choice.
export const DEFAULT_SANDBOX_IMAGE = 'agentic-sandbox:0.1'

// Registry references get long (`swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`)
// and the recognisable part is the tail, so the org prefix is dropped first and only then is
// the remainder truncated. Always pair with `title={image}` so the full value stays available.
export function shortImage(image: string, max = 28): string {
  const tail = image.includes('/') ? image.slice(image.lastIndexOf('/') + 1) : image
  return tail.length <= max ? tail : `${tail.slice(0, max - 1)}…`
}

// What to show for a run's image. `null` means the run predates the column — those runs did
// use the generic image, but it was never recorded, so this returns null and the caller shows
// nothing rather than back-filling a claim we cannot support.
export function runImageLabel(image?: string | null): string | null {
  if (!image) return null
  return image === DEFAULT_SANDBOX_IMAGE ? 'default sandbox' : shortImage(image)
}
