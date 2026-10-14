import { useState, useEffect } from 'react';
import { api } from '../api';
import { useNotify } from '../context/NotificationContext';
import { Button } from './ui/Button';
import { Card } from './ui/Card';

const FIELD = 'w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-teal-300 dark:bg-gray-800';

export default function PresendSettings() {
  const notify = useNotify();
  const [url, setUrl] = useState('');
  const [secret, setSecret] = useState('');
  const [secretSet, setSecretSet] = useState(false);
  const [timeout, setTimeoutSeconds] = useState('5');
  const [tokenField, setTokenField] = useState('veltiono_token');
  const [source, setSource] = useState('off');
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [origUrl, setOrigUrl] = useState('');

  const load = () => api.get('/settings/presend').then(d => {
    setUrl(d.url || '');
    setOrigUrl(d.url || '');
    setLoaded(true);
    setLoadError('');
    setSecretSet(!!d.secret_set);
    setTimeoutSeconds(String(d.timeout_seconds ?? 5));
    setTokenField(d.token_field || 'veltiono_token');
    setSource(d.source || 'off');
    setSecret('');
  }).catch(err => setLoadError(err?.message || 'Failed to load pre-send settings'));

  useEffect(() => { load(); }, []);

  const save = async payload => {
    setSaving(true);
    try {
      await api.post('/settings/presend', payload);
      notify({ type: 'success', message: 'Pre-send check saved' });
      await load();
    } catch (err) {
      notify({ type: 'error', message: err.message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="mb-4 space-y-3">
      <div className="flex items-center gap-3">
        <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-100">Pre-send check</h3>
        <span className="text-[10px] bg-gray-100 text-gray-500 border rounded-full px-2 py-0.5 font-medium">
          {source === 'off' ? 'Off' : `On (${source})`}
        </span>
      </div>
      <p className="text-sm text-gray-500">
        Before every email Quickly asks this URL; the email is sent only on an explicit yes.
        Use "Disable pre-send check" to turn the check off.
      </p>
      {loadError && <p className="text-sm text-red-600">Could not load settings: {loadError}</p>}
      <label className="block text-sm font-medium text-gray-700">URL</label>
      <input className={FIELD} value={url} onChange={e => setUrl(e.target.value)} placeholder="https://…/api/partners/outreach/may-send" />
      <label className="block text-sm font-medium text-gray-700">Secret</label>
      <input
        type="password"
        className={FIELD}
        value={secret}
        onChange={e => setSecret(e.target.value)}
        placeholder={secretSet ? 'leave empty to keep the stored secret' : ''}
        autoComplete="new-password"
      />
      <label className="block text-sm font-medium text-gray-700">Timeout (seconds)</label>
      <input className={FIELD} value={timeout} onChange={e => setTimeoutSeconds(e.target.value)} />
      <label className="block text-sm font-medium text-gray-700">Token field (lead custom field holding the token)</label>
      <input className={FIELD} value={tokenField} onChange={e => setTokenField(e.target.value)} />
      <div className="flex gap-2">
        <Button
          type="button"
          disabled={saving || !loaded}
          onClick={() => {
            const payload = { secret, timeout_seconds: parseFloat(timeout) || 5, token_field: tokenField };
            if (url.trim() && url !== origUrl) payload.url = url.trim();
            save(payload);
          }}
        >
          Save
        </Button>
        <Button type="button" variant="outline" disabled={saving || !loaded} onClick={() => save({ enabled: false })}>
          Disable pre-send check
        </Button>
        <Button type="button" variant="outline" disabled={saving || !loaded} onClick={() => save({ reset: true })}>
          Reset to environment
        </Button>
      </div>
    </Card>
  );
}
