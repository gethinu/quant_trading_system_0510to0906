'use client';

import { useState } from 'react';

/**
 * 既定で閉じている詳細セクション。
 *
 * 設計の要点:
 *   1. **閉じていても状態が分かる**。見出しの右に要約値 (`meta`) を出すので、開かずに
 *      「累計 +$2,966 / 652本」までは読める。情報を消すのではなく畳んでいる。
 *   2. **開くまで DOM に出さない** (lazy mount)。決済履歴 400 行や保有 61 行を閉じたまま
 *      DOM に置くと、スマホで scroll が重くなるし静的 HTML も肥大する。
 *   3. 一度開いたら閉じても unmount しない。表のフィルタ・ソート状態が畳むたびに
 *      リセットすると使い物にならないため、2 回目以降は `hidden` で隠すだけにする。
 */
interface Props {
  title: string;
  /** 閉じたままでも状態が分かる 1 行 (見出しの右)。 */
  meta?: React.ReactNode;
  /** 件数などの強調 chip。 */
  badge?: { text: string; tone: 'fail' | 'warn' | 'ok' | 'muted' };
  defaultOpen?: boolean;
  children: React.ReactNode;
}

const BADGE_TONE: Record<string, string> = {
  fail: 'bg-fail/20 text-fail',
  warn: 'bg-warn/20 text-warn',
  ok: 'bg-ok/15 text-ok',
  muted: 'bg-white/10 text-muted',
};

export function Collapsible({ title, meta, badge, defaultOpen = false, children }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const [mounted, setMounted] = useState(defaultOpen);

  const toggle = () => {
    setOpen((v) => {
      if (!v) setMounted(true);
      return !v;
    });
  };

  return (
    <section className="bg-card rounded-xl shadow-lg overflow-hidden">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-white/[0.03] transition-colors"
      >
        <span
          aria-hidden="true"
          className={`text-muted text-[10px] leading-none transition-transform ${
            open ? 'rotate-90' : ''
          }`}
        >
          ▶
        </span>
        <span className="text-xs uppercase tracking-widest text-muted shrink-0">
          {title}
        </span>
        {badge ? (
          <span
            className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium tabular-nums ${
              BADGE_TONE[badge.tone]
            }`}
          >
            {badge.text}
          </span>
        ) : null}
        {meta ? (
          <span className="ml-auto text-right text-[11px] text-muted tabular-nums truncate">
            {meta}
          </span>
        ) : null}
      </button>
      {mounted ? (
        <div hidden={!open} className="px-4 pb-4 pt-1 border-t border-white/5">
          {children}
        </div>
      ) : null}
    </section>
  );
}

export default Collapsible;
