/**
 * One version's composition change as inline badges: what was added, what
 * was dropped, and what was reweighted -- the thing a basket's whole
 * "version history" is actually for.
 */
import type { ChangeSummary } from '@/api/basket'
import { Badge } from '@/components/ui/badge'

interface Props {
  summary: ChangeSummary
  /** The first version has nothing to diff against -- everything is new. */
  isFirstVersion?: boolean
}

export function ChangeSummaryBadges({ summary, isFirstVersion }: Props) {
  if (isFirstVersion) {
    return (
      <div className="flex flex-wrap gap-1.5">
        <Badge variant="outline" className="border-sky-500/40 text-sky-500">
          Initial composition
        </Badge>
      </div>
    )
  }

  const nothing =
    summary.added.length === 0 && summary.removed.length === 0 && summary.reweighted.length === 0

  if (nothing) {
    return <span className="text-xs text-muted-foreground">No composition change</span>
  }

  return (
    <div className="flex flex-wrap gap-1.5">
      {summary.added.map((symbol) => (
        <Badge
          key={`added-${symbol}`}
          className="border-emerald-500/40 bg-emerald-500/10 text-emerald-500"
          variant="outline"
        >
          +{symbol}
        </Badge>
      ))}
      {summary.removed.map((symbol) => (
        <Badge
          key={`removed-${symbol}`}
          variant="outline"
          className="border-rose-500/40 bg-rose-500/10 text-rose-500 line-through"
        >
          {symbol}
        </Badge>
      ))}
      {summary.reweighted.map((r) => (
        <Badge key={`reweighted-${r.symbol}`} variant="outline" className="text-muted-foreground">
          {r.symbol} {r.from.toFixed(1)}%&rarr;{r.to.toFixed(1)}%
        </Badge>
      ))}
    </div>
  )
}
