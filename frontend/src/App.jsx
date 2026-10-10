import { useEffect, useRef, useState } from 'react'
import './App.css'

const api = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')
const number = (value) => Number(value || 0).toFixed(2)

async function request(path, options = {}) {
  const response = await fetch(`${api}${path}`, {
    ...options,
    signal: options.signal
      ? AbortSignal.any([options.signal, AbortSignal.timeout(60000)])
      : AbortSignal.timeout(60000),
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('An authorized operator token is required.')
    if (response.status === 409) throw new Error('Another simulation is running. Refresh and try again.')
    throw new Error(`Service request failed (${response.status}). Check backend connectivity.`)
  }
  return response.json()
}

function App() {
  const [state, setState] = useState(null)
  const [logs, setLogs] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [token, setToken] = useState('')
  const [online, setOnline] = useState(false)
  const latestRevision = useRef(-1)
  const generation = useRef(null)

  function acceptState(next) {
    if (next.generation_id !== generation.current) {
      generation.current = next.generation_id
      latestRevision.current = -1
    }
    if (next.revision >= latestRevision.current) {
      latestRevision.current = next.revision
      setState(next)
    }
  }

  useEffect(() => {
    let active = true
    let timer
    const controller = new AbortController()
    async function refresh() {
      try {
        const [next, events] = await Promise.all([
          request('/api/state', { signal: controller.signal }),
          request('/api/logs', { signal: controller.signal }),
        ])
        if (active) {
          acceptState(next)
          setLogs(events.logs)
          setOnline(true)
          setError('')
        }
      } catch (err) {
        if (active) {
          setOnline(false)
          setError(err.message)
        }
      } finally {
        if (active) timer = setTimeout(refresh, 3000)
      }
    }
    refresh()
    return () => {
      active = false
      clearTimeout(timer)
      controller.abort()
    }
  }, [])

  async function simulate(event) {
    setBusy(true)
    setError('')
    try {
      const next = await request(event === 'reset' ? '/api/reset' : '/api/disruptions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { 'X-API-Key': token } : {}),
        },
        ...(event === 'reset' ? {} : { body: JSON.stringify({ event }) }),
      })
      acceptState(next)
      const events = await request('/api/logs')
      setLogs(events.logs)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main>
      <header>
        <div className="brand"><span className="brand-icon">ϟ</span><div>
          <p className="eyebrow">COMMUNITY ENERGY INTELLIGENCE</p>
          <h1>Micro-Grid <span>Resiliency Agent</span></h1>
        </div></div>
        <div className={`connection ${online ? 'online' : ''}`} role="status">
          <span className="dot" />{online ? 'Live connection' : 'Connecting / offline'}
        </div>
      </header>
      <section className="intro">
        <div><h2>Stronger together. Powered locally.</h2>
          <p>Autonomous households negotiate energy sharing to keep essential services running.</p></div>
        <span className={`badge ${state?.grid_connected ? 'balanced' : 'shedding'}`}>
          {state ? (state.grid_connected ? 'Grid connected' : 'Islanded micro-grid') : 'Awaiting grid'}
        </span>
      </section>
      {error && <div className="error" role="alert">{error}</div>}
      <section className="metrics" aria-label="Community metrics">
        {[
          ['Solar generation', state?.totals?.solar_kw, 'kW', '☀'],
          ['Active demand', state?.totals?.load_kw, 'kW', 'ϟ'],
          ['Stored energy', state?.totals?.battery_kwh, 'kWh', '▣'],
          ['Unserved this round', state?.totals?.unserved_kwh, 'kWh', '◇'],
        ].map(([label, value, unit, icon]) => <article className="metric" key={label}>
          <span className="metric-icon">{icon}</span><p>{label}</p>
          <strong>{state ? number(value) : '—'} <small>{unit}</small></strong>
        </article>)}
      </section>
      <div className="workspace">
        <section className="panel neighborhood">
          <div className="panel-heading"><div><h3>Neighborhood network</h3>
            <p>Energy links · {state?.households?.length || 0} autonomous households</p></div>
            <span className="subtle">{state?.event || 'initializing'}</span></div>
          <div className="map" aria-label="Neighborhood energy sharing map">
            <svg viewBox="0 0 600 260" role="img" aria-label="Community energy exchange network">
              <defs><pattern id="grid" width="25" height="25" patternUnits="userSpaceOnUse">
                <path d="M 25 0 L 0 0 0 25" fill="none" stroke="#233446" strokeWidth="0.5" />
              </pattern></defs>
              <rect width="600" height="260" fill="url(#grid)" />
              {(state?.households || []).map((house, index, all) => {
                const angle = index * Math.PI * 2 / all.length - Math.PI / 2
                const x = 300 + Math.cos(angle) * 205
                const y = 130 + Math.sin(angle) * 88
                const participating = (state?.trades || []).some(t => t.seller_id === house.id || t.buyer_id === house.id)
                return <g key={house.id}>
                  <line x1="300" y1="130" x2={x} y2={y} stroke={participating ? '#52d9ad' : '#3b5164'} strokeDasharray={participating ? '0' : '5 5'} />
                  <circle cx={x} cy={y} r="21" fill="#172a38" stroke={house.status === 'shedding' ? '#ffbb65' : '#52d9ad'} />
                  <text x={x} y={y + 6} textAnchor="middle" fill="#e8f2f4" fontSize="20">⌂</text>
                  <text x={x} y={y + 38} textAnchor="middle" fill="#b1c8d5" fontSize="11">{house.name}</text>
                </g>
              })}
              <circle cx="300" cy="130" r="32" fill="#153e38" stroke="#52d9ad" />
              <text x="300" y="128" textAnchor="middle" fill="#63e4bb" fontSize="22">ϟ</text>
              <text x="300" y="143" textAnchor="middle" fill="#e8f2f4" fontSize="8">COORDINATOR</text>
            </svg>
          </div>
          <div className="households">
            {(state?.households || []).map(house => <article className="house" key={house.id}>
              <div className="house-title"><h4>{house.name}</h4><span className={`badge ${house.status}`}>{house.status}</span></div>
              <div className="house-values"><span>Solar <b>{number(house.solar_kw)} kW</b></span><span>Load <b>{number(house.load_kw)} kW</b></span></div>
              <div className="battery-label"><span>Battery</span><b>{number(house.battery_pct)}%</b></div>
              <progress max="100" value={house.battery_pct} aria-label={`${house.name} battery`} />
              <p className="tier">Active load tier <strong>{house.active_load_tier}</strong></p>
            </article>)}
            {!state && <p className="empty">Connecting to the community coordinator…</p>}
          </div>
        </section>
        <aside className="panel controls">
          <p className="eyebrow">RESILIENCY LAB</p><h3>Put the grid to the test</h3>
          <p>Each event advances one {state?.simulation_minutes || 15}-minute round. Agents rebalance supply, share batteries, and prioritize critical loads.</p>
          <label htmlFor="operator-token">Operator token <span>(if enabled)</span></label>
          <input id="operator-token" type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} placeholder="Kept in memory only" />
          <button disabled={busy || !state || !online} onClick={() => simulate('storm')}>☁ Simulate storm</button>
          <button className="outage" disabled={busy || !state || !online} onClick={() => simulate('outage')}>ϟ Simulate grid outage</button>
          <button className="secondary" disabled={busy || !state || !online} onClick={() => simulate('reset')}>↻ Reset community</button>
          <div className="simulation-status" role="status">{busy ? 'Agents are negotiating…' : 'Ready for the next scenario'}</div>
          <div className="note"><b>Transparent by design</b><p>Gemini JSON decisions are validated against physical limits. Without AI credentials, clearly labeled deterministic agents keep the demo fully runnable.</p></div>
        </aside>
      </div>
      <section className="panel logs">
        <div className="panel-heading"><div><h3>Agent negotiation console</h3><p>Structured decisions & community ledger · newest first</p></div><span className="subtle">{logs.length} events</span></div>
        <div className="log-console" aria-label="Agent negotiation logs">
          {logs.length === 0 && <p className="empty">No negotiations yet. Trigger a disruption to start a round.</p>}
          {[...logs].reverse().map(log => <details key={log.id}>
            <summary><time>{new Date(log.timestamp).toLocaleTimeString()}</time><span className="log-agent">{log.household_id || log.agent || 'coordinator'}</span><span className={`source ${log.source}`}>{log.source}</span><span>{log.message}</span></summary>
            <pre>{JSON.stringify(log.payload, null, 2)}</pre>
          </details>)}
        </div>
      </section>
      <footer><span>Micro-Grid Community Energy Trading & Resiliency Agent</span><span>{state ? `Revision ${state.revision} · Updated ${new Date(state.updated_at).toLocaleTimeString()}` : 'Waiting for backend'}</span></footer>
    </main>
  )
}

export default App
