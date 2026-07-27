import type { Behind } from '@/lib/freshness';

/**
 * 「配信中のデータが古い」警告バナー (2026-07-22 の publish 取りこぼし incident 対策)。
 *
 * 判定ロジックは lib/freshness.ts に移し、ここは表示だけを持つ。呼び出し側
 * (StatusSummary) が `useFreshness()` の結果を渡す。
 * behind=null (= 遅れていない / まだ突き合わせていない) の時は何も描かない。
 */
export function FreshnessBanner({
  date,
  behind,
}: {
  date: string | null;
  behind: Behind | null;
}) {
  if (!date || !behind) return null;

  return (
    <div
      role="alert"
      className="mb-2 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn"
    >
      <span className="font-semibold">⚠ データが古い可能性</span>{' '}
      <span className="text-cardfg">
        表示 {date} / 想定 {behind.expected}（{behind.days} 営業日遅れ）
      </span>
      。ダッシュの publish が取りこぼされた可能性があります。最新は ntfy を確認してください。
    </div>
  );
}

export default FreshnessBanner;
