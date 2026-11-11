/**
 * Candle Breach Scan Node
 * Scans every closed candle in a look-back window for the first one whose
 * high went above, or low went below, a level - so a breach between two runs
 * is not missed
 */

import { Handle, Position } from '@xyflow/react'
import { ScanSearch } from 'lucide-react'
import { memo } from 'react'
import { cn } from '@/lib/utils'
import type { CandleScanNodeData } from '@/types/flow'

interface CandleScanNodeProps {
  data: CandleScanNodeData
  selected?: boolean
}

export const CandleScanNode = memo(({ data, selected }: CandleScanNodeProps) => {
  const hasAbove = data.above !== undefined && data.above !== null && data.above !== ''
  const hasBelow = data.below !== undefined && data.below !== null && data.below !== ''
  return (
    <div className={cn('workflow-node min-w-[130px] border-l-primary', selected && 'selected')}>
      <Handle type="target" position={Position.Top} className="!top-0 !-translate-y-1/2" />
      <div className="p-2">
        <div className="mb-1.5 flex items-center gap-1.5">
          <div className="flex h-5 w-5 items-center justify-center rounded bg-primary/20 text-primary">
            <ScanSearch className="h-3 w-3" />
          </div>
          <div>
            <div className="text-xs font-medium leading-tight">Candle Breach Scan</div>
            <div className="text-[9px] text-muted-foreground">
              {data.interval || '1m'}, last {data.lookbackMinutes ?? 120} min
            </div>
          </div>
        </div>
        <div className="space-y-1">
          <div className="flex items-center justify-between rounded bg-muted/50 px-1.5 py-1">
            <span className="text-[10px] text-muted-foreground">Symbol</span>
            <span className="mono-data text-[10px] font-medium">{data.symbol || '-'}</span>
          </div>
          {hasAbove && (
            <div className="flex items-center justify-between rounded bg-muted/50 px-1.5 py-1">
              <span className="text-[10px] text-muted-foreground">High above</span>
              <span className="mono-data text-[10px] font-medium">{String(data.above)}</span>
            </div>
          )}
          {hasBelow && (
            <div className="flex items-center justify-between rounded bg-muted/50 px-1.5 py-1">
              <span className="text-[10px] text-muted-foreground">Low below</span>
              <span className="mono-data text-[10px] font-medium">{String(data.below)}</span>
            </div>
          )}
        </div>
        {data.outputVariable && (
          <div className="mt-1 text-center text-[9px] text-muted-foreground">
            {data.outputVariable}
          </div>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} className="!bottom-0 !translate-y-1/2" />
    </div>
  )
})

CandleScanNode.displayName = 'CandleScanNode'
