import { Copy, Eye, EyeOff, Key, KeyRound, Plus, Star, Trash2, Unplug } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { webClient } from '@/api/client'
import { Alert, AlertDescription } from '@/components/ui/alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { showToast } from '@/utils/toast'

interface BrokerAccount {
  account_id: string
  broker: string
  label: string
  is_default: boolean
  connected: boolean
  has_api_key: boolean
}

export default function BrokerAccountsTab() {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([])
  const [validBrokers, setValidBrokers] = useState<string[]>([])
  const [isLoading, setIsLoading] = useState(true)

  const [showAddDialog, setShowAddDialog] = useState(false)
  const [newBroker, setNewBroker] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const [newApiKey, setNewApiKey] = useState('')
  const [newApiSecret, setNewApiSecret] = useState('')
  const [isCreating, setIsCreating] = useState(false)

  const [pendingDelete, setPendingDelete] = useState<BrokerAccount | null>(null)
  const [revealedKeys, setRevealedKeys] = useState<Record<string, string>>({})
  const [busyAccountId, setBusyAccountId] = useState<string | null>(null)

  const [editingAccount, setEditingAccount] = useState<BrokerAccount | null>(null)
  const [editApiKey, setEditApiKey] = useState('')
  const [editApiSecret, setEditApiSecret] = useState('')
  const [isSavingCredentials, setIsSavingCredentials] = useState(false)

  const fetchAccounts = useCallback(async () => {
    try {
      const response = await webClient.get<{ status: string; data: BrokerAccount[] }>(
        '/api/accounts'
      )
      setAccounts(response.data.data || [])
    } catch {
      showToast.error('Failed to load broker accounts', 'system')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAccounts()
    webClient
      .get<{ status: string; data: { valid_brokers: string[] } }>('/api/broker/credentials')
      .then((response) => setValidBrokers(response.data.data?.valid_brokers || []))
      .catch(() => undefined)
  }, [fetchAccounts])

  const handleAddAccount = async () => {
    if (!newBroker) {
      showToast.error('Select a broker', 'system')
      return
    }
    setIsCreating(true)
    try {
      const response = await webClient.post<{
        status: string
        message?: string
        data?: { account_id: string; connect_url: string }
      }>('/api/accounts', {
        broker: newBroker,
        label: newLabel || undefined,
        broker_api_key: newApiKey || undefined,
        broker_api_secret: newApiSecret || undefined,
      })

      if (response.data.status === 'success' && response.data.data) {
        showToast.success('Account created — redirecting to connect...', 'system')
        // Hand off to the existing broker OAuth/TOTP login flow, now scoped
        // to this new account_id (carried via the server-side session).
        window.location.href = response.data.data.connect_url
        return
      }
      showToast.error(response.data.message || 'Failed to create account', 'system')
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        'Failed to create account'
      showToast.error(message, 'system')
    } finally {
      setIsCreating(false)
      setShowAddDialog(false)
      setNewBroker('')
      setNewLabel('')
      setNewApiKey('')
      setNewApiSecret('')
    }
  }

  const handleDelete = async () => {
    if (!pendingDelete) return
    setBusyAccountId(pendingDelete.account_id)
    try {
      await webClient.delete(`/api/accounts/${pendingDelete.account_id}`)
      showToast.success('Account removed', 'system')
      await fetchAccounts()
    } catch {
      showToast.error('Failed to remove account', 'system')
    } finally {
      setBusyAccountId(null)
      setPendingDelete(null)
    }
  }

  const handleSetDefault = async (account: BrokerAccount) => {
    setBusyAccountId(account.account_id)
    try {
      await webClient.post(`/api/accounts/${account.account_id}/default`)
      showToast.success(`${account.label} set as default`, 'system')
      await fetchAccounts()
    } catch {
      showToast.error('Failed to set default account', 'system')
    } finally {
      setBusyAccountId(null)
    }
  }

  const handleConnect = async (account: BrokerAccount) => {
    setBusyAccountId(account.account_id)
    try {
      const response = await webClient.post<{
        status: string
        data?: { connect_url: string }
      }>(`/api/accounts/${account.account_id}/connect`)
      const connectUrl = response.data.data?.connect_url
      if (connectUrl) {
        window.location.href = connectUrl
        return
      }
      showToast.error('Failed to start broker connection', 'system')
    } catch {
      showToast.error('Failed to start broker connection', 'system')
    } finally {
      setBusyAccountId(null)
    }
  }

  const handleSaveCredentials = async () => {
    if (!editingAccount) return
    if (!editApiKey && !editApiSecret) {
      showToast.error('Enter an API key or secret to update', 'system')
      return
    }
    setIsSavingCredentials(true)
    try {
      await webClient.post(`/api/accounts/${editingAccount.account_id}/credentials`, {
        broker_api_key: editApiKey || undefined,
        broker_api_secret: editApiSecret || undefined,
      })
      showToast.success('Broker credentials updated', 'system')
      setEditingAccount(null)
      setEditApiKey('')
      setEditApiSecret('')
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        'Failed to update credentials'
      showToast.error(message, 'system')
    } finally {
      setIsSavingCredentials(false)
    }
  }

  const handleGenerateKey = async (account: BrokerAccount) => {
    setBusyAccountId(account.account_id)
    try {
      const response = await webClient.post<{
        status: string
        data?: { api_key: string }
      }>(`/api/accounts/${account.account_id}/apikey`)
      const apiKey = response.data.data?.api_key
      if (apiKey) {
        setRevealedKeys((prev) => ({ ...prev, [account.account_id]: apiKey }))
        showToast.success('API key generated', 'system')
        await fetchAccounts()
      }
    } catch {
      showToast.error('Failed to generate API key', 'system')
    } finally {
      setBusyAccountId(null)
    }
  }

  const handleCopyKey = async (apiKey: string) => {
    try {
      await navigator.clipboard.writeText(apiKey)
      showToast.success('API key copied to clipboard', 'clipboard')
    } catch {
      showToast.error('Failed to copy API key', 'clipboard')
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <Alert>
        <Key className="h-4 w-4" />
        <AlertDescription>
          Connect multiple broker accounts to this login — including more than one account on the
          same broker. Each account gets its own AlgoZ API key; incoming requests route to the
          correct account automatically based on which key is used.
        </AlertDescription>
      </Alert>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>Broker Accounts</CardTitle>
            <CardDescription>Accounts connected to your AlgoZ login</CardDescription>
          </div>
          <Button size="sm" onClick={() => setShowAddDialog(true)}>
            <Plus className="h-4 w-4 mr-2" />
            Add Account
          </Button>
        </CardHeader>
        <CardContent>
          {accounts.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              No broker accounts yet. Click "Add Account" to connect one.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead>Broker</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>API Key</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {accounts.map((account) => (
                  <TableRow key={account.account_id}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        {account.label}
                        {account.is_default && (
                          <Badge variant="outline" className="gap-1">
                            <Star className="h-3 w-3" />
                            Default
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{account.broker.toUpperCase()}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={account.connected ? 'default' : 'outline'}>
                        {account.connected ? 'Connected' : 'Not connected'}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {revealedKeys[account.account_id] ? (
                        <div className="flex items-center gap-2">
                          <code className="text-xs">
                            {revealedKeys[account.account_id].slice(0, 8)}
                            {'•'.repeat(16)}
                          </code>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6"
                            onClick={() => handleCopyKey(revealedKeys[account.account_id])}
                            title="Copy API key"
                          >
                            <Copy className="h-3 w-3" />
                          </Button>
                        </div>
                      ) : account.has_api_key ? (
                        <span className="text-xs text-muted-foreground">
                          Generated (regenerate to view)
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">Not generated</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        {!account.connected && (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={busyAccountId === account.account_id}
                            onClick={() => handleConnect(account)}
                          >
                            <Unplug className="h-4 w-4 mr-1" />
                            Connect
                          </Button>
                        )}
                        {!account.is_default && (
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8"
                            disabled={busyAccountId === account.account_id}
                            onClick={() => handleSetDefault(account)}
                            title="Set as default"
                          >
                            <Star className="h-4 w-4" />
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          disabled={busyAccountId === account.account_id}
                          onClick={() => {
                            setEditingAccount(account)
                            setEditApiKey('')
                            setEditApiSecret('')
                          }}
                          title="Edit broker API key/secret"
                        >
                          <KeyRound className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          disabled={busyAccountId === account.account_id}
                          onClick={() => handleGenerateKey(account)}
                          title={account.has_api_key ? 'Regenerate API key' : 'Generate API key'}
                        >
                          {revealedKeys[account.account_id] ? (
                            <EyeOff className="h-4 w-4" />
                          ) : (
                            <Eye className="h-4 w-4" />
                          )}
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 text-destructive"
                          disabled={busyAccountId === account.account_id}
                          onClick={() => setPendingDelete(account)}
                          title="Remove account"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Add Account Dialog */}
      <Dialog open={showAddDialog} onOpenChange={setShowAddDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Broker Account</DialogTitle>
            <DialogDescription>
              Connect a new broker account. You'll be redirected to complete the broker's login
              after creating it.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>Broker</Label>
              <Select value={newBroker} onValueChange={setNewBroker}>
                <SelectTrigger>
                  <SelectValue placeholder="Select a broker" />
                </SelectTrigger>
                <SelectContent>
                  {validBrokers.map((broker) => (
                    <SelectItem key={broker} value={broker}>
                      {broker.charAt(0).toUpperCase() + broker.slice(1)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Label (optional)</Label>
              <Input
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                placeholder="e.g. Personal, Family, Trading Desk"
              />
            </div>
            <div className="pt-2 border-t space-y-4">
              <p className="text-xs text-muted-foreground">
                Optional: this account's own broker app API key/secret (only needed if it differs
                from the instance-wide default configured in the Broker tab — required when
                connecting a second account on the same broker).
              </p>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Broker API Key</Label>
                  <Input
                    type="password"
                    value={newApiKey}
                    onChange={(e) => setNewApiKey(e.target.value)}
                    placeholder="Leave blank to use default"
                  />
                </div>
                <div className="space-y-2">
                  <Label>Broker API Secret</Label>
                  <Input
                    type="password"
                    value={newApiSecret}
                    onChange={(e) => setNewApiSecret(e.target.value)}
                    placeholder="Leave blank to use default"
                  />
                </div>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowAddDialog(false)}>
              Cancel
            </Button>
            <Button onClick={handleAddAccount} disabled={isCreating || !newBroker}>
              {isCreating ? 'Creating...' : 'Create & Connect'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Credentials Dialog */}
      <Dialog
        open={!!editingAccount}
        onOpenChange={(open) => {
          if (!open) {
            setEditingAccount(null)
            setEditApiKey('')
            setEditApiSecret('')
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Credentials — {editingAccount?.label}</DialogTitle>
            <DialogDescription>
              Update this account's broker app API key/secret. Leave a field blank to keep its
              current value. Existing values are never shown here — only replaced.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>Broker API Key</Label>
              <Input
                type="password"
                value={editApiKey}
                onChange={(e) => setEditApiKey(e.target.value)}
                placeholder="Leave blank to keep current value"
              />
            </div>
            <div className="space-y-2">
              <Label>Broker API Secret</Label>
              <Input
                type="password"
                value={editApiSecret}
                onChange={(e) => setEditApiSecret(e.target.value)}
                placeholder="Leave blank to keep current value"
              />
            </div>
            {!editingAccount?.connected && (editApiKey || editApiSecret) && (
              <p className="text-xs text-muted-foreground">
                After saving, click "Connect" on this account to use the new credentials.
              </p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditingAccount(null)}>
              Cancel
            </Button>
            <Button
              onClick={handleSaveCredentials}
              disabled={isSavingCredentials || (!editApiKey && !editApiSecret)}
            >
              {isSavingCredentials ? 'Saving...' : 'Save Credentials'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation */}
      <AlertDialog open={!!pendingDelete} onOpenChange={(open) => !open && setPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove {pendingDelete?.label}?</AlertDialogTitle>
            <AlertDialogDescription>
              This revokes the broker session and API key for this account. Strategies using its API
              key will stop working until you reconnect.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete}>Remove</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
