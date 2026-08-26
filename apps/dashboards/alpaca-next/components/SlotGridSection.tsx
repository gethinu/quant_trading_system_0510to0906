import type { PoolMeter, SlotFlowModel, SystemSlotRow } from '@/lib/slotModel';

/**
 * 枠 (スロット) ビュー — このダッシュボードの一番上に置く主役。
 *
 * 「S4 は候補 10 なのに Entry 0」を見た人が、**推測せずに**「ロング枠が満杯だから」
 * と読めることだけを目的にしている。したがって
 *   1. 先に 1 文で答えを書く (headline)
 *   2. long / short / 合計 の 3 メーターで「空きが何枠あったか」を出す
 *   3. system ごとに ■保有 / ▨本日 / □空き を並べ、右に落ちた理由を 1 行
 * の順に降りる。密度を上げないため、詳細 (自己検査・ファネル) は畳んである。
 */

function Meter({ meter }: { meter: PoolMeter }) {
  const heldPct = meter.cap > 0 ? Math.min(100, (meter.held / meter.cap) * 100) : 0;
  const addPct =
    meter.cap > 0
      ? Math.min(100 - heldPct, (Math.min(meter.kept, meter.allow) / meter.cap) * 100)
      : 0;
  const isShort = meter.side === 'short';
  return (
    <div className="rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[11px] uppercase tracking-widest text-muted">{meter.label}</span>
        <span className="text-[10px] text-muted tabular-nums">上限 {meter.cap}</span>
      </div>
      <div className="mt-1 flex items-baseline gap-1.5 tabular-nums">
        <span className="text-2xl font-semibold leading-none">{meter.held}</span>
        <span className="text-[11px] text-muted">保有</span>
        <span className={`text-lg font-semibold leading-none ${isShort ? 'text-fail/70' : 'text-ok/70'}`}>
          +{meter.kept}
        </span>
        <span className="text-[11px] text-muted">本日</span>
        <span className="ml-auto text-[11px] text-muted">空き {meter.allow}</span>
      </div>
      <div className="mt-2 flex h-2 w-full overflow-hidden rounded-full bg-white/[0.07]">
        <div
          className={isShort ? 'h-full bg-fail' : 'h-full bg-ok'}
          style={{ width: `${heldPct}%` }}
        />
        <div
          className={isShort ? 'h-full bg-fail/45' : 'h-full bg-ok/45'}
          style={{ width: `${addPct}%` }}
        />
      </div>
      <div
        className={`mt-1.5 text-[10px] leading-snug ${meter.exhausted ? 'text-warn' : 'text-muted'}`}
      >
        {meter.exhausted ? '満杯 · ' : ''}
        {meter.note}
      </div>
    </div>
  );
}

function SlotBoxes({ row, postEntry }: { row: SystemSlotRow; postEntry: boolean }) {
  const isShort = row.spec.side === 'short';
  const filled = row.spec.side === 'short' ? 'bg-fail border-fail' : 'bg-ok border-ok';
  const added = isShort ? 'bg-fail/45 border-fail/45' : 'bg-ok/45 border-ok/45';
  const boxes = [];
  const heldCount = Math.min(row.held, row.spec.slots);
  const newCount = postEntry ? 0 : Math.min(row.netEntries, Math.max(0, row.spec.slots - heldCount));
  for (let i = 0; i < row.spec.slots; i += 1) {
    const cls =
      i < heldCount ? filled : i < heldCount + newCount ? added : 'bg-transparent border-white/20';
    boxes.push(
      <span
        key={i}
        aria-hidden="true"
        className={`inline-block h-3.5 w-3.5 rounded-[3px] border ${cls}`}
      />,
    );
  }
  for (let i = 0; i < row.over; i += 1) {
    boxes.push(
      <span
        key={`over-${i}`}
        aria-hidden="true"
        className="inline-block h-3.5 w-3.5 rounded-[3px] border border-warn bg-warn"
      />,
    );
  }
  return <div className="flex flex-wrap gap-[3px]">{boxes}</div>;
}

const TONE_CLASS: Record<SystemSlotRow['why']['tone'], string> = {
  ok: 'text-ok',
  warn: 'text-warn',
  blocked: 'text-fail',
  muted: 'text-muted',
};

function SystemRow({ row, postEntry }: { row: SystemSlotRow; postEntry: boolean }) {
  const dimmed = row.candidates === 0 && row.held === 0;
  return (
    <div
      className={`border-t border-white/5 py-3 first:border-t-0 ${dimmed ? 'opacity-55' : ''}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-semibold tabular-nums">{row.spec.short}</span>
        <span className="text-[11px] text-muted">{row.spec.desc}</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] ${
            row.spec.side === 'short' ? 'bg-fail/15 text-fail' : 'bg-ok/15 text-ok'
          }`}
        >
          {row.spec.side}
        </span>
        <span className="ml-auto text-[11px] tabular-nums text-muted">
          <span className="text-base font-semibold text-cardfg">{row.used}</span>/{row.spec.slots}
        </span>
        {row.over > 0 ? (
          <span className="rounded bg-warn/20 px-1.5 py-0.5 text-[10px] text-warn">
            枠超過 +{row.over}
          </span>
        ) : null}
      </div>

      <div className="mt-2">
        <SlotBoxes row={row} postEntry={postEntry} />
      </div>

      <div className={`mt-2 text-[12px] leading-snug ${TONE_CLASS[row.why.tone]}`}>
        {row.why.text}
      </div>

      {row.skipSummary.length > 0 ? (
        <div className="mt-1 text-[11px] leading-snug text-muted">
          発注 skip:{' '}
          {row.skipSummary.map((s, i) => (
            <span key={s.category}>
              {i > 0 ? ' / ' : ''}
              <span className="text-warn">{s.label}</span> {s.count}
            </span>
          ))}
          <span className="tabular-nums"> → 実際に枠を増やすのは {row.netEntries} 本</span>
        </div>
      ) : null}

      <div className="mt-1 text-[10px] tabular-nums text-muted/80">
        優先度 {row.spec.priority} · 配分 {(row.spec.weight * 100).toFixed(0)}% · {row.spec.order}
      </div>
    </div>
  );
}

function GroupHeader({ meter }: { meter: PoolMeter | undefined }) {
  if (!meter) return null;
  return (
    <div className="mt-1 rounded bg-white/[0.05] px-2 py-1 text-[10px] uppercase tracking-widest text-muted">
      {meter.label} — 保有 {meter.held} / 上限 {meter.cap} · 空き {meter.allow}
    </div>
  );
}

export function SlotGridSection({ model }: { model: SlotFlowModel }) {
  if (model.unavailable) {
    return (
      <section className="rounded-xl bg-card p-4 shadow-lg">
        <h2 className="text-xs uppercase tracking-widest text-muted">枠 (スロット)</h2>
        <p className="mt-2 text-sm text-warn">{model.unavailable}</p>
      </section>
    );
  }
  const longMeter = model.meters.find((m) => m.key === 'long');
  const shortMeter = model.meters.find((m) => m.key === 'short');
  const postEntry = model.basis.holdings === 'post_entry';
  const longRows = model.rows.filter((r) => r.spec.side === 'long');
  const shortRows = model.rows.filter((r) => r.spec.side === 'short');

  return (
    <section className="rounded-xl bg-card p-4 shadow-lg">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-xs uppercase tracking-widest text-muted">
          枠 — long / short × system
        </h2>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] ${
            postEntry
              ? 'border-warn/40 text-warn'
              : 'border-ok/40 text-ok'
          }`}
        >
          {postEntry ? '保有 = 引け後実測（本日分を含む）' : '保有 = エントリー前の実測'}
        </span>
        <span className="ml-auto text-[10px] tabular-nums text-muted">{model.date}</span>
      </div>

      {model.headline ? (
        <p className="mt-3 rounded-lg border-l-2 border-sky-400/70 bg-white/[0.04] px-3 py-2 text-[13px] leading-relaxed">
          {model.headline}
        </p>
      ) : null}

      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        {model.meters.map((m) => (
          <Meter key={m.key} meter={m} />
        ))}
      </div>

      <GroupHeader meter={longMeter} />
      <div className="mt-1">
        {longRows.map((row) => (
          <SystemRow key={row.spec.id} row={row} postEntry={postEntry} />
        ))}
        {model.orphan ? (
          <div className="border-t border-white/5 py-3">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-semibold text-muted">orphan</span>
              <span className="text-[11px] text-muted">system 帰属なし (delisted 等)</span>
              <span className="ml-auto text-[11px] tabular-nums text-muted">
                <span className="text-base font-semibold text-cardfg">{model.orphan.count}</span> 枠
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-[3px]">
              {model.orphan.symbols.map((s) => (
                <span
                  key={s}
                  aria-hidden="true"
                  className="inline-block h-3.5 w-3.5 rounded-[3px] border border-white/25 bg-white/25"
                />
              ))}
            </div>
            <div className="mt-2 text-[12px] text-warn">
              ロング枠を {model.orphan.count} つ占有する（system 枠には数えられない）
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              {model.orphan.symbols.map((s) => (
                <span
                  key={s}
                  className="rounded border border-white/10 px-1 text-[10px] tabular-nums text-muted"
                >
                  {s}
                </span>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      <GroupHeader meter={shortMeter} />
      <div className="mt-1">
        {shortRows.map((row) => (
          <SystemRow key={row.spec.id} row={row} postEntry={postEntry} />
        ))}
      </div>

      <div className="mt-3 text-[10px] leading-relaxed text-muted">
        <span className="mr-1 inline-block h-2.5 w-2.5 rounded-[2px] bg-ok align-middle" />
        既保有で埋まっている枠
        <span className="ml-3 mr-1 inline-block h-2.5 w-2.5 rounded-[2px] bg-ok/45 align-middle" />
        本日のエントリーで埋まる枠
        <span className="ml-3 mr-1 inline-block h-2.5 w-2.5 rounded-[2px] border border-white/20 align-middle" />
        空き枠
        <span className="ml-3 mr-1 inline-block h-2.5 w-2.5 rounded-[2px] bg-warn align-middle" />
        枠超過（現在のコードでは強制されていない）
      </div>
      <div className="mt-1 text-[10px] leading-relaxed text-muted">
        出所: 枠メーター = today_signals.portfolio.caps ·{' '}
        {model.basis.holdingsSource ? `保有 = ${model.basis.holdingsSource}` : '保有 = 未取得'} ·{' '}
        {model.basis.entriesSource ? `エントリー = ${model.basis.entriesSource}` : 'エントリー = 未取得'}
        {' · '}
        <span className={model.basis.sidecarVerified ? 'text-ok' : 'text-muted'}>
          {model.basis.sidecarVerified
            ? 'sidecar は bundle manifest の hash で検証済み'
            : 'sidecar は日付一致のみ（hash 未検証）'}
        </span>
      </div>

      <details className="mt-3 rounded-lg border border-white/10">
        <summary className="cursor-pointer select-none list-none px-3 py-2 text-[11px] text-muted">
          ▸ 枠モデル（コード上の正）と本日値の自己検査
        </summary>
        <div className="overflow-x-auto px-3 pb-3">
          <table className="w-full text-[11px] tabular-nums">
            <thead className="text-muted">
              <tr className="text-left">
                <th className="py-1 pr-2 font-medium">system</th>
                <th className="py-1 pr-2 font-medium">side</th>
                <th className="py-1 pr-2 text-right font-medium">枠</th>
                <th className="py-1 pr-2 text-right font-medium">優先度</th>
                <th className="py-1 pr-2 text-right font-medium">配分</th>
                <th className="py-1 font-medium">発注</th>
              </tr>
            </thead>
            <tbody>
              {model.rows.map((r) => (
                <tr key={r.spec.id} className="border-t border-white/5">
                  <td className="py-1 pr-2">{r.spec.short}</td>
                  <td className="py-1 pr-2 text-muted">{r.spec.side}</td>
                  <td className="py-1 pr-2 text-right">{r.spec.slots}</td>
                  <td className="py-1 pr-2 text-right">{r.spec.priority}</td>
                  <td className="py-1 pr-2 text-right">{(r.spec.weight * 100).toFixed(0)}%</td>
                  <td className="py-1 text-muted">{r.spec.order}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[10px] leading-relaxed text-muted">
            優先度 = side 昇順 → system 番号昇順。この並びのまま末尾から捨てられるので、
            <span className="text-cardfg"> S5 と S7 が構造的に最初の犠牲</span>になる。
            ロングとショートは別プール（{longMeter?.cap ?? '—'} と {shortMeter?.cap ?? '—'}）で、
            合計 {model.caps?.caps?.max_total ?? '—'} が最後に効く。
          </p>

          <h3 className="mt-3 text-[11px] font-medium text-muted">自己検査</h3>
          <table className="mt-1 w-full text-[11px]">
            <tbody>
              {model.selfChecks.map((c) => (
                <tr key={c.label} className="border-t border-white/5">
                  <td className="py-1 pr-2">{c.label}</td>
                  <td className="py-1 pr-2 tabular-nums text-muted">{c.value}</td>
                  <td className={`py-1 text-right font-medium ${c.pass ? 'text-ok' : 'text-fail'}`}>
                    {c.pass ? '一致' : '不一致'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[10px] leading-relaxed text-muted">
            出所: config/config.yaml risk.max_positions · risk.portfolio ／ config/settings.py
            ui.long_allocations · short_allocations ／ core/final_allocation.py
            _resolve_max_positions · _load_portfolio_caps · _apply_portfolio_caps ·
            _sort_final_frame。表示側の定数が実行時とズレたら、上の自己検査が「不一致」になる。
          </p>
        </div>
      </details>
    </section>
  );
}

export default SlotGridSection;
