import type { ExitItem, SlotFlowModel, SystemSlotRow } from '@/lib/slotModel';

/**
 * フロービュー — 昨日ポジション → 今日エグジット(−) → 今日エントリー(+) → 今日ポジション。
 *
 * 読ませたいのは「連続性」なので、**4 つの数字を必ず横 1 行**に置く (スマホでも
 * 折り返さない)。銘柄チップはその下に別ブロックで積む。行の右肩に
 * `y − out + in = now` の算術をそのまま出して、数字の出どころを消さない。
 *
 * 重要な区別を 2 つ、必ず画面に出す:
 *   - エグジットは **決済 (time_based / flatten_all / 保護注文の約定) だけ** を − に
 *     数える。その日に置いただけの protect_* は、置いた日にポジションを減らさない。
 *   - 「今日ポジション」は引け後 (22:5x) にしか実測が出ない。朝は
 *     昨日 − 決済 + 新規 の **見込み**。どちらなのかを毎回ラベルする。
 */

function Chip({ text, tone = 'muted' }: { text: string; tone?: 'muted' | 'warn' | 'out' }) {
  const cls =
    tone === 'warn'
      ? 'border-warn/50 text-warn'
      : tone === 'out'
        ? 'border-white/10 text-muted/60 line-through'
        : 'border-white/10 text-muted';
  return (
    <span className={`rounded border px-1 text-[10px] leading-[1.5] tabular-nums ${cls}`}>
      {text}
    </span>
  );
}

function Num({
  label,
  value,
  tone,
  sub,
}: {
  label: string;
  value: string;
  tone?: 'plus' | 'minus';
  sub?: string;
}) {
  return (
    <div className="rounded-md bg-white/[0.04] px-1.5 py-1.5 text-center">
      <div className="text-[9px] leading-tight tracking-wide text-muted/70">{label}</div>
      <div
        className={`text-lg font-semibold leading-tight tabular-nums ${
          tone === 'plus' ? 'text-ok' : tone === 'minus' ? 'text-fail' : ''
        }`}
      >
        {value}
      </div>
      {sub ? <div className="text-[9px] leading-tight text-muted/70">{sub}</div> : null}
    </div>
  );
}

function ChipRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mt-1.5 flex gap-2">
      <span className="w-14 shrink-0 pt-[1px] text-[9px] uppercase tracking-wide text-muted/70">
        {label}
      </span>
      <span className="flex flex-wrap gap-1">{children}</span>
    </div>
  );
}

function FlowRow({
  row,
  measured,
  postEntry,
}: {
  row: SystemSlotRow;
  measured: boolean;
  postEntry: boolean;
}) {
  const y = row.held;
  const out = row.closes.length;
  const inn = row.netEntries;
  const projected = row.projectedNow;
  const hasMeasured = measured && row.measuredNow != null;
  const now = hasMeasured ? (row.measuredNow as number) : projected;
  const empty = y === 0 && out === 0 && row.entries.length === 0;
  const skipped = row.entries.filter((e) => e.skipReason != null);

  return (
    <div className={`border-t border-white/5 py-3 ${empty ? 'opacity-50' : ''}`}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-semibold">{row.spec.short}</span>
        <span className="text-[11px] text-muted">{row.spec.desc}</span>
        {postEntry ? null : (
          <span className="ml-auto text-[10px] tabular-nums text-muted">
            {y} − {out} + {inn} = {projected}
            {hasMeasured && row.measuredNow !== projected ? ` ／ 実測 ${row.measuredNow}` : ''}
          </span>
        )}
      </div>

      <div className="mt-2 grid grid-cols-4 gap-1.5">
        <Num label={postEntry ? '現在の保有' : '昨日'} value={String(y)} />
        <Num
          label="エグジット"
          value={out > 0 ? `−${out}` : '0'}
          tone={out > 0 ? 'minus' : undefined}
        />
        <Num
          label="エントリー"
          value={inn > 0 ? `+${inn}` : '0'}
          tone={inn > 0 ? 'plus' : undefined}
          sub={skipped.length > 0 ? `提案 ${row.entries.length}` : undefined}
        />
        <Num
          label={measured ? '今日 実測' : '今日 見込み'}
          value={String(now)}
          sub={measured || postEntry ? undefined : `${y}−${out}+${inn}`}
        />
      </div>

      {row.heldSymbols.length > 0 ? (
        <ChipRow label={postEntry ? '保有' : '昨日'}>
          {row.heldSymbols.map((s) => (
            <Chip key={s} text={s} />
          ))}
        </ChipRow>
      ) : null}

      {row.closes.length > 0 ? (
        <ChipRow label="決済 −">
          {row.closes.map((e: ExitItem) => (
            <Chip key={`${e.symbol}-${e.reason}`} text={e.symbol} tone="out" />
          ))}
        </ChipRow>
      ) : null}

      {row.entries.length > 0 ? (
        <ChipRow label="新規 +">
          {row.entries.map((e) => (
            <Chip key={e.symbol} text={e.symbol} tone={e.skipReason ? 'warn' : 'muted'} />
          ))}
        </ChipRow>
      ) : null}

      {skipped.length > 0 ? (
        <div className="mt-1 pl-16 text-[10px] leading-snug text-warn">
          提案 {row.entries.length} 本のうち {skipped.length} 本は発注時に skip → 実質 +{inn}
        </div>
      ) : null}
      {row.entries.length === 0 && (row.candidates ?? 0) > 0 ? (
        <div className="mt-1 pl-16 text-[10px] text-fail">候補 {row.candidates} → 枠切れで 0</div>
      ) : null}
      {row.closedBeforeRead.length > 0 ? (
        <div className="mt-1 pl-16 text-[10px] leading-snug text-muted/80">
          {row.closedBeforeRead.map((e) => e.symbol).join(' · ')}{' '}
          は左の観測より前に決済済み（昨日の数から既に抜けている）
        </div>
      ) : null}
      {row.protects.length > 0 ? (
        <div className="mt-1 pl-16 text-[10px] leading-snug text-muted/80">
          保護注文 {row.protects.length} 件（置くだけ・ポジションは減らない）
        </div>
      ) : null}
    </div>
  );
}

export function FlowSection({ model }: { model: SlotFlowModel }) {
  if (model.unavailable) return null;
  const measured = model.basis.todayBook === 'measured';
  // 寄り前 (エントリー前) の保有 read が publish されていない日は、左端が
  // 「昨日」ではなく「本日分を含む現在」になる。連続性の算術は成立しないので、
  // 名前も算術も出さずに、何がどう違うのかを先に断る。
  const postEntry = model.basis.holdings === 'post_entry';
  const t = model.totals;
  const now = measured && t.measuredNow != null ? t.measuredNow : t.projectedNow;
  const diff = !postEntry && measured && t.measuredNow != null
    ? t.measuredNow - t.projectedNow
    : 0;

  return (
    <section className="rounded-xl bg-card p-4 shadow-lg">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-xs uppercase tracking-widest text-muted">
          フロー — 昨日 → 今日の出入り → 今日
        </h2>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] ${
            measured ? 'border-ok/40 text-ok' : 'border-warn/40 text-warn'
          }`}
        >
          {measured ? '今日ポジション = 引け後実測' : '今日ポジション = 見込み（引け前）'}
        </span>
        <span className="ml-auto text-[10px] tabular-nums text-muted">{model.date}</span>
      </div>

      {postEntry ? (
        <p className="mt-2 rounded border border-warn/25 bg-warn/10 px-2 py-1.5 text-[11px] leading-snug text-warn">
          エントリー前のポジション実測（<span className="tabular-nums">exit_orders_*_proposal</span>）が
          この日は publish されていないため、<b>昨日 → 今日 の連続性は出せません</b>。左端は
          「本日のエントリーを含む現在の保有」です。
        </p>
      ) : !measured ? (
        <p className="mt-2 rounded border border-warn/25 bg-warn/10 px-2 py-1.5 text-[11px] leading-snug text-warn">
          「今日ポジション」は <b>昨日 − 決済 + 新規</b> の見込みです。実測スナップショットは
          当日 22:5x にしか生成されないため、引け前は確定値を出せません。
        </p>
      ) : null}

      <div className="mt-2">
        {model.rows.map((row) => (
          <FlowRow key={row.spec.id} row={row} measured={measured} postEntry={postEntry} />
        ))}

        {model.orphan ? (
          <div className="border-t border-white/5 py-3 opacity-75">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-semibold text-muted">orphan</span>
              <span className="text-[11px] text-muted">system 帰属なし</span>
              <span className="ml-auto text-[10px] tabular-nums text-muted">
                {model.orphan.count} − 0 + 0 = {model.orphan.count}
              </span>
            </div>
            <div className="mt-2 grid grid-cols-4 gap-1.5">
              <Num label={postEntry ? '現在の保有' : '昨日'} value={String(model.orphan.count)} />
              <Num label="エグジット" value="0" />
              <Num label="エントリー" value="0" />
              <Num
                label={measured ? '今日 実測' : '今日 見込み'}
                value={String(model.orphan.count)}
              />
            </div>
            <ChipRow label="昨日">
              {model.orphan.symbols.map((s) => (
                <Chip key={s} text={s} />
              ))}
            </ChipRow>
          </div>
        ) : null}

        <div className="mt-1 border-t border-white/10 pt-3">
          <div className="mb-1 text-[11px] font-semibold">合計</div>
          <div className="grid grid-cols-4 gap-1.5">
            <Num label={postEntry ? '現在の保有' : '昨日'} value={String(t.held)} />
            <Num
              label="エグジット"
              value={t.closes > 0 ? `−${t.closes}` : '0'}
              tone={t.closes > 0 ? 'minus' : undefined}
              sub={t.protects > 0 ? `保護 ${t.protects}` : undefined}
            />
            <Num
              label="エントリー"
              value={t.entriesNet > 0 ? `+${t.entriesNet}` : '0'}
              tone={t.entriesNet > 0 ? 'plus' : undefined}
              sub={
                t.entriesProposed !== t.entriesNet
                  ? `提案 ${t.entriesProposed} / skip ${t.entriesProposed - t.entriesNet}`
                  : undefined
              }
            />
            <Num
              label={measured ? '今日 実測' : '今日 見込み'}
              value={String(now)}
              sub={measured || postEntry ? undefined : `${t.held}−${t.closes}+${t.entriesNet}`}
            />
          </div>
          {measured && diff !== 0 ? (
            <div className="mt-1.5 text-[10px] leading-snug text-warn">
              見込み {t.projectedNow} との差 {diff > 0 ? '+' : '−'}
              {Math.abs(diff)} — 当日中に約定しなかった指値エントリーなど。
            </div>
          ) : null}
          {t.closedBeforeRead > 0 ? (
            <div className="mt-1 text-[10px] leading-snug text-muted/80">
              左の観測より前に決済された {t.closedBeforeRead} 件は、昨日の数から既に抜けている。
            </div>
          ) : null}
        </div>
      </div>

      <div className="mt-3 text-[10px] leading-relaxed text-muted">
        ・<span className="text-warn">黄色の銘柄</span> は発注時に skip されたエントリー（既保有 /
        system 枠上限など）。提案には出るが枠は増えない。
        <br />
        ・「エグジット」は決済（time_based / flatten_all / 保護注文の約定）だけを − に数える。当日
        置いただけの protect_stop / protect_target / protect_trailing は、置いた日にポジションを減らさない。
      </div>
      <div className="mt-1 text-[10px] leading-relaxed text-muted">
        出所: 保有 = {model.basis.holdingsSource ?? '—'} ／ エントリー ={' '}
        {model.basis.entriesSource ?? '—'} ／ エグジット = {model.basis.exitsSource ?? 'なし'}
        {model.basis.holdingsObservedAt ? ` ・ broker read ${model.basis.holdingsObservedAt}` : ''}
      </div>
    </section>
  );
}

export default FlowSection;
