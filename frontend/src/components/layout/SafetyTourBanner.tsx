import { useState } from "react"
import { X } from "lucide-react"
import { Link } from "react-router"
import { readSafetyTourDismissed, writeSafetyTourDismissed } from "@/lib/safety-tour"

/**
 * One-time safety tour (docs/archive/UI_REWRITE_PLAN.md §14) - dismissible,
 * shown once ever (localStorage), independent of the onboarding wizard
 * (which only appears until config.yaml exists at all).
 */
export function SafetyTourBanner() {
  const [dismissed, setDismissed] = useState(readSafetyTourDismissed)

  if (dismissed) return null

  return (
    <div className="flex w-full items-center justify-between gap-2 border-b border-info/20 bg-info/7 px-4 py-2 text-sm">
      <span>
        High-impact capabilities are off by default. Enable them in{" "}
        <Link to="/access" className="underline underline-offset-2 hover:text-foreground">
          Access
        </Link>
        .
      </span>
      <button
        type="button"
        aria-label="Dismiss"
        className="text-muted-foreground hover:text-foreground"
        onClick={() => {
          writeSafetyTourDismissed()
          setDismissed(true)
        }}
      >
        <X className="size-4" />
      </button>
    </div>
  )
}
