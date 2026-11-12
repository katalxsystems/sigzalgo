// components/flow/panels/ExecutionLogPanel.tsx
// Displays execution logs: the run just started from the editor, and the saved
// history of every run (schedule, webhook, price alert, order update, Run Now).

import { useQuery } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, Clock, Terminal, X, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { flowQueryKeys, getWorkflowExecutions } from '@/api/flow'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'

export interface LogEntry {
  time: string
  message: string
  level: 'info' | 'error' | 'warning'
  node?: string
}

type PanelStatus = 'idle' | 'running' | 'success' | 'error'

interface ExecutionLogPanelProps {
  workflowId: number
  /** Logs of the run started from this editor ("Run Now"), if any */
  logs: LogEntry[]
  status: PanelStatus
  onClose: () => void
}

const LIVE = 'live'

function toPanelStatus(status: string): PanelStatus {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'running' || status === 'pending') return 'running'
  return 'idle'
}

export function ExecutionLogPanel({
  workflowId,
  logs: liveLogs,
  status: liveStatus,
  onClose,
}: ExecutionLogPanelProps) {
  const hasLiveRun = liveStatus !== 'idle'
  const { data: executions = [] } = useQuery({
    queryKey: flowQueryKeys.executions(workflowId),
    queryFn: () => getWorkflowExecutions(workflowId, 20),
    refetchInterval: 15000,
  })
  const [selected, setSelected] = useState<string>(LIVE)

  // Show the editor's own run when there is one, else the latest saved run.
  useEffect(() => {
    if (hasLiveRun) {
      setSelected(LIVE)
    } else if (selected === LIVE && executions.length > 0) {
      setSelected(String(executions[0].id))
    }
  }, [hasLiveRun, executions, selected])

  const saved = executions.find((e) => String(e.id) === selected)
  const logs: LogEntry[] = saved ? ((saved.logs ?? []) as LogEntry[]) : liveLogs
  const status: PanelStatus = saved ? toPanelStatus(saved.status) : liveStatus

  const formatRunLabel = (startedAt: string | null, runStatus: string, runId: number) => {
    const when = startedAt
      ? new Date(startedAt).toLocaleString('en-IN', {
          day: '2-digit',
          month: 'short',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
        })
      : `Run #${runId}`
    return `${when} - ${runStatus}`
  }
  const getStatusIcon = () => {
    switch (status) {
      case 'running':
        return <Clock className="h-4 w-4 animate-pulse text-amber-500" />
      case 'success':
        return <CheckCircle2 className="h-4 w-4 text-green-500" />
      case 'error':
        return <XCircle className="h-4 w-4 text-red-500" />
      default:
        return <Terminal className="h-4 w-4 text-muted-foreground" />
    }
  }

  const getStatusText = () => {
    switch (status) {
      case 'running':
        return 'Executing...'
      case 'success':
        return 'Completed'
      case 'error':
        return 'Failed'
      default:
        return 'Ready'
    }
  }

  const formatTime = (isoString: string) => {
    try {
      const date = new Date(isoString)
      return date.toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      })
    } catch {
      return isoString
    }
  }

  const getLevelIcon = (level: string) => {
    switch (level) {
      case 'error':
        return <XCircle className="h-3.5 w-3.5 text-red-500" />
      case 'warning':
        return <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
      default:
        return <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
    }
  }

  return (
    <div className="w-80 border-l border-border bg-card flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          {getStatusIcon()}
          <span className="font-medium">Execution Log</span>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={cn(
              'text-xs px-2 py-0.5 rounded-full',
              status === 'running' && 'bg-amber-500/10 text-amber-500',
              status === 'success' && 'bg-green-500/10 text-green-500',
              status === 'error' && 'bg-red-500/10 text-red-500',
              status === 'idle' && 'bg-muted text-muted-foreground'
            )}
          >
            {getStatusText()}
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={onClose}
            aria-label="Close execution log"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Run picker: this editor's run and saved history */}
      {(hasLiveRun || executions.length > 0) && (
        <div className="border-b border-border px-4 py-2">
          <label htmlFor="execution-run" className="sr-only">
            Execution
          </label>
          <select
            id="execution-run"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
          >
            {hasLiveRun && <option value={LIVE}>This run</option>}
            {executions.map((e) => (
              <option key={e.id} value={String(e.id)}>
                {formatRunLabel(e.started_at, e.status, e.id)}
              </option>
            ))}
          </select>
          {saved?.error && <p className="mt-1 text-xs text-red-500 break-words">{saved.error}</p>}
        </div>
      )}

      {/* Log entries */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="p-3 space-y-2">
          {logs.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground text-sm">
              <Terminal className="h-8 w-8 mx-auto mb-2 opacity-50" />
              <p>No logs yet</p>
              <p className="text-xs mt-1">
                {saved
                  ? 'This run recorded no log entries'
                  : 'Click "Run Now", or wait for a scheduled or webhook run'}
              </p>
            </div>
          ) : (
            logs.map((log, index) => (
              <div
                key={index}
                className={cn(
                  'rounded-lg border p-2.5 text-sm',
                  log.level === 'error' && 'border-red-500/30 bg-red-500/5',
                  log.level === 'warning' && 'border-amber-500/30 bg-amber-500/5',
                  log.level === 'info' && 'border-border bg-background'
                )}
              >
                <div className="flex items-start gap-2">
                  <div className="mt-0.5">{getLevelIcon(log.level)}</div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs text-muted-foreground font-mono">
                        {formatTime(log.time)}
                      </span>
                    </div>
                    <p
                      className={cn(
                        'text-sm break-words',
                        log.level === 'error' && 'text-red-500',
                        log.level === 'warning' && 'text-amber-500'
                      )}
                    >
                      {log.message}
                    </p>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </ScrollArea>

      {/* Footer with summary */}
      {logs.length > 0 && (
        <div className="border-t border-border px-4 py-2 text-xs text-muted-foreground">
          <div className="flex justify-between">
            <span>{logs.length} log entries</span>
            <span>{logs.filter((l) => l.level === 'error').length} errors</span>
          </div>
        </div>
      )}
    </div>
  )
}
