'use client';

import { useState } from 'react';

interface TabsProps {
  signalsView: React.ReactNode;
  alpacaView: React.ReactNode;
  signalsBadge?: string;
  alpacaBadge?: string;
  /** tone for the alpaca badge (today P&L up/down). */
  alpacaBadgeTone?: 'up' | 'down' | 'flat';
  defaultTab?: 'signals' | 'alpaca';
}

/**
 * Client-side tab switcher. Both views are rendered on the server and passed in
 * as slots, so static export is preserved — only which slot is shown toggles on
 * the client. Signals tab wraps the existing (unchanged) signals UI.
 */
export function Tabs({
  signalsView,
  alpacaView,
  signalsBadge,
  alpacaBadge,
  alpacaBadgeTone = 'flat',
  defaultTab = 'signals',
}: TabsProps) {
  const [tab, setTab] = useState<'signals' | 'alpaca'>(defaultTab);

  const toneCls =
    alpacaBadgeTone === 'up'
      ? 'bg-ok/20 text-ok'
      : alpacaBadgeTone === 'down'
      ? 'bg-fail/20 text-fail'
      : 'bg-white/10 text-muted';

  const TabButton = ({
    id,
    label,
    badge,
    badgeCls,
  }: {
    id: 'signals' | 'alpaca';
    label: string;
    badge?: string;
    badgeCls?: string;
  }) => {
    const active = tab === id;
    return (
      <button
        role="tab"
        aria-selected={active}
        onClick={() => setTab(id)}
        className={[
          'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors',
          active
            ? 'bg-white/10 text-cardfg'
            : 'text-muted hover:text-cardfg hover:bg-white/5',
        ].join(' ')}
      >
        <span>{label}</span>
        {badge ? (
          <span
            className={`px-1.5 py-0.5 rounded-full text-[10px] tabular-nums ${
              badgeCls ?? 'bg-white/10 text-muted'
            }`}
          >
            {badge}
          </span>
        ) : null}
      </button>
    );
  };

  return (
    <div>
      <div
        role="tablist"
        className="sticky top-0 z-20 -mx-4 sm:-mx-6 px-4 sm:px-6 py-2 mb-4 flex items-center gap-1 bg-[#0f1220]/85 backdrop-blur border-b border-white/5"
      >
        <TabButton id="signals" label="Signals" badge={signalsBadge} />
        <TabButton
          id="alpaca"
          label="Alpaca"
          badge={alpacaBadge}
          badgeCls={toneCls}
        />
      </div>
      <div role="tabpanel">{tab === 'signals' ? signalsView : alpacaView}</div>
    </div>
  );
}

export default Tabs;
