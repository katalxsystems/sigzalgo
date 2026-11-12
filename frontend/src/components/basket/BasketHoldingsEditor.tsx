/**
 * Basket-scoped holdings editor: symbol rows with a weight that rescales the
 * rest to keep the total at 100 on every edit, same interaction as the
 * Portfolio Backtester's builder. Kept as its own copy rather than shared
 * with PortfolioBacktester.tsx -- that page is well-tested and in real use,
 * and factoring its inline logic out risks regressing it for a small DRY
 * win. This component is what both the create-basket and add-rebalance
 * dialogs use.
 */
import { useMemo } from 'react'
import type { BasketHolding } from '@/api/basket'
import { SymbolSearchInput } from '@/components/portfolio/SymbolSearchInput'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'

interface Props {
  holdings: BasketHolding[]
  onChange(holdings: BasketHolding[]): void
}

export function BasketHoldingsEditor({ holdings, onChange }: Props) {
  const totalWeight = useMemo(
    () => holdings.reduce((s, h) => s + (Number(h.weight) || 0), 0),
    [holdings]
  )

  const setHolding = (i: number, patch: Partial<BasketHolding>) =>
    onChange(holdings.map((h, n) => (n === i ? { ...h, ...patch } : h)))

  const setWeight = (i: number, raw: number) => {
    const newWeight = Math.max(0, Math.min(100, Number.isFinite(raw) ? raw : 0))
    const remaining = 100 - newWeight
    const othersSum = holdings.reduce((s, h, n) => (n === i ? s : s + (Number(h.weight) || 0)), 0)
    const otherCount = holdings.length - 1
    onChange(
      holdings.map((h, n) => {
        if (n === i) return { ...h, weight: newWeight }
        const w = Number(h.weight) || 0
        const scaled =
          othersSum > 0 ? (w * remaining) / othersSum : otherCount > 0 ? remaining / otherCount : 0
        return { ...h, weight: Number(scaled.toFixed(2)) }
      })
    )
  }

  const addHolding = () => {
    const newWeight = Number((100 / (holdings.length + 1)).toFixed(2))
    const remaining = 100 - newWeight
    const existingSum = holdings.reduce((s, h) => s + (Number(h.weight) || 0), 0)
    const scaled = holdings.map((h) => ({
      ...h,
      weight:
        existingSum > 0
          ? Number(((Number(h.weight) || 0) * (remaining / existingSum)).toFixed(2))
          : Number((remaining / holdings.length).toFixed(2)),
    }))
    onChange([...scaled, { symbol: '', exchange: 'NSE', weight: newWeight }])
  }

  const distributeEqually = () =>
    onChange(holdings.map((h) => ({ ...h, weight: Number((100 / holdings.length).toFixed(2)) })))

  const removeHolding = (i: number) => onChange(holdings.filter((_, n) => n !== i))

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">Holdings</span>
        <div className="flex gap-2">
          <Button type="button" variant="outline" size="sm" onClick={addHolding}>
            Add Stock
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={distributeEqually}
            disabled={holdings.length === 0}
          >
            Distribute Equally
          </Button>
        </div>
      </div>

      {holdings.map((h, i) => (
        <div key={`row-${i}-${h.symbol}`} className="flex items-center gap-2">
          <SymbolSearchInput
            className="w-56"
            value={h.symbol}
            exchange={h.exchange as 'NSE' | 'BSE'}
            onSelect={(sym) => setHolding(i, { symbol: sym })}
          />
          <Select value={h.exchange} onValueChange={(v) => setHolding(i, { exchange: v })}>
            <SelectTrigger className="w-20">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="NSE">NSE</SelectItem>
              <SelectItem value="BSE">BSE</SelectItem>
            </SelectContent>
          </Select>
          <Input
            className="w-20"
            type="number"
            value={h.weight}
            onChange={(e) => setWeight(i, Number(e.target.value))}
          />
          <span className="text-xs text-muted-foreground">%</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={holdings.length <= 1}
            onClick={() => removeHolding(i)}
          >
            Remove
          </Button>
        </div>
      ))}

      <div className="flex items-center justify-end">
        <span
          className={cn(
            'text-sm tabular-nums',
            Math.abs(totalWeight - 100) < 0.01 ? 'text-emerald-500' : 'text-muted-foreground'
          )}
        >
          Total {totalWeight.toFixed(1)}%
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        Weights are normalised, so they need not total 100. A symbol left out of this list that was
        held before is sold out entirely as of this date.
      </p>
    </div>
  )
}
