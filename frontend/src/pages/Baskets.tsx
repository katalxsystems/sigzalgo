/**
 * Portfolio Baskets — list page.
 *
 * A basket is a named, saved allocation you come back to and rebalance
 * manually over time, unlike the Portfolio Backtester's one-shot "try this
 * weighting" runs.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import {
  type BasketHolding,
  basketQueryKeys,
  type CreateBasketRequest,
  createBasket,
  listBaskets,
} from '@/api/basket'
import { listBenchmarks } from '@/api/portfolio'
import { BasketHoldingsEditor } from '@/components/basket/BasketHoldingsEditor'
import { ChangeSummaryBadges } from '@/components/basket/ChangeSummaryBadges'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAuthStore } from '@/stores/authStore'

const todayISO = () => new Date().toISOString().slice(0, 10)

const fmtDate = (iso: string | null) =>
  iso
    ? new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        timeZone: 'UTC',
      })
    : '-'

function extractErrorMessage(err: unknown): string {
  const e = err as { response?: { data?: { message?: unknown } }; message?: string }
  const msg = e.response?.data?.message ?? e.message ?? 'request failed'
  return typeof msg === 'string' ? msg : JSON.stringify(msg)
}

export default function Baskets() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { apiKey } = useAuthStore()

  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [holdings, setHoldings] = useState<BasketHolding[]>([
    { symbol: '', exchange: 'NSE', weight: 100 },
  ])
  const [effectiveDate, setEffectiveDate] = useState(todayISO())
  const [benchmark, setBenchmark] = useState('none')
  const [initialCapital, setInitialCapital] = useState(100000)
  const [error, setError] = useState<string | null>(null)

  const { data: baskets = [], isLoading } = useQuery({
    queryKey: basketQueryKeys.list(),
    queryFn: () => listBaskets(apiKey ?? ''),
    enabled: !!apiKey,
  })

  const { data: benchmarks = [] } = useQuery({
    queryKey: ['portfolio', 'benchmarks'],
    queryFn: () => listBenchmarks(apiKey ?? ''),
    enabled: !!apiKey && createOpen,
    staleTime: 5 * 60_000,
  })

  const resetForm = () => {
    setName('')
    setHoldings([{ symbol: '', exchange: 'NSE', weight: 100 }])
    setEffectiveDate(todayISO())
    setBenchmark('none')
    setInitialCapital(100000)
    setError(null)
  }

  const createMutation = useMutation({
    mutationFn: (req: CreateBasketRequest) => createBasket(req),
    onSuccess: (basket) => {
      queryClient.invalidateQueries({ queryKey: basketQueryKeys.list() })
      setCreateOpen(false)
      resetForm()
      navigate(`/baskets/${basket.id}`)
    },
    onError: (err: unknown) => setError(extractErrorMessage(err)),
  })

  const submitCreate = () => {
    if (!apiKey) {
      setError('No API key found. Generate one on the API Key page.')
      return
    }
    if (!name.trim()) {
      setError('Name the basket.')
      return
    }
    const cleanHoldings = holdings.filter((h) => h.symbol.trim())
    if (cleanHoldings.length === 0) {
      setError('Add at least one holding.')
      return
    }
    setError(null)
    createMutation.mutate({
      apikey: apiKey,
      name: name.trim(),
      holdings: cleanHoldings,
      effective_date: effectiveDate,
      benchmark: benchmark === 'none' ? null : benchmark,
      benchmark_exchange: benchmarks.find((b) => b.symbol === benchmark)?.exchange ?? 'NSE_INDEX',
      initial_capital: initialCapital,
    })
  }

  return (
    <div className="container mx-auto space-y-4 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Portfolio Baskets</h1>
          <p className="text-sm text-muted-foreground">
            Saved, weighted baskets you rebalance manually over time and track against a benchmark.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="mr-1.5 h-4 w-4" />
          Create Basket
        </Button>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading baskets…</p>}

      {!isLoading && baskets.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
            <p className="text-sm text-muted-foreground">
              No baskets yet. Create one to start tracking a manually-rebalanced allocation against
              a benchmark.
            </p>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus className="mr-1.5 h-4 w-4" />
              Create Basket
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {baskets.map((basket) => (
          <Card
            key={basket.id}
            className="cursor-pointer transition-colors hover:border-primary/40"
            onClick={() => navigate(`/baskets/${basket.id}`)}
          >
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="text-base">{basket.name}</CardTitle>
                {basket.benchmark && <Badge variant="outline">{basket.benchmark}</Badge>}
              </div>
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="flex justify-between text-xs text-muted-foreground">
                <span>
                  {basket.version_count} version{basket.version_count === 1 ? '' : 's'}
                </span>
                <span>as of {fmtDate(basket.latest_effective_date ?? null)}</span>
              </div>
              <p className="text-xs text-muted-foreground">Updated {fmtDate(basket.updated_at)}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Dialog
        open={createOpen}
        onOpenChange={(open) => {
          setCreateOpen(open)
          if (!open) resetForm()
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Create Basket</DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs">Name</Label>
                <Input value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div>
                <Label className="text-xs">Effective date</Label>
                <Input
                  type="date"
                  value={effectiveDate}
                  onChange={(e) => setEffectiveDate(e.target.value)}
                />
              </div>
            </div>

            <BasketHoldingsEditor holdings={holdings} onChange={setHoldings} />

            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs">Benchmark</Label>
                <Select value={benchmark} onValueChange={setBenchmark}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">None</SelectItem>
                    {benchmarks.map((b) => (
                      <SelectItem key={`${b.exchange}-${b.symbol}`} value={b.symbol}>
                        {b.symbol}{' '}
                        <span className="text-muted-foreground">
                          {b.exchange.replace('_INDEX', '')}
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label className="text-xs">Initial capital</Label>
                <Input
                  type="number"
                  value={initialCapital}
                  onChange={(e) => setInitialCapital(Number(e.target.value))}
                />
              </div>
            </div>

            {holdings.filter((h) => h.symbol.trim()).length > 0 && (
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">Preview</Label>
                <ChangeSummaryBadges
                  summary={{ added: [], removed: [], reweighted: [] }}
                  isFirstVersion
                />
              </div>
            )}

            {error && <p className="text-sm text-rose-500">{error}</p>}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button onClick={submitCreate} disabled={createMutation.isPending}>
              {createMutation.isPending ? 'Creating…' : 'Create Basket'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
