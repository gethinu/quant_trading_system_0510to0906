/**
 * 確定 delisted（上場廃止・API から成行 close 不能）銘柄の恒久レジストリ。
 *
 * なぜ必要か:
 *   snapshot exporter の delisted 判定は Alpaca の live asset-status probe に依存する。
 *   probe が degrade した場合（あるいは実行中の exporter＝open-auto-run が旧版で
 *   delisted 判定そのものを持たない場合）、確定 delisted 銘柄が snapshot 上で
 *   ``system="unknown"`` に化ける。この揮発で FOLD/CDTX が alloc チャート・期限超過・
 *   赤/黄アラートに「偽ノイズ」として復活していた（2026-08-03 snapshot で再現）。
 *
 *   ダッシュはこのレジストリを truth として参照し、probe の生死に関係なく
 *   確定 delisted を安定して除外・ラベル付けする。
 *
 * 収録基準（捏造しない）:
 *   - 実判定で delisted と確認できた銘柄のみ。
 *   - FOLD / CDTX: 2026-07-30 の live probe が inactive/非 tradable = delisted と観測
 *     （published snapshot 07-30 で system="delisted"）＋ 成行 close 不能をユーザー確認。
 *   - MF は含めない: delisted 未確定。entry-coid ``system3-MF-20260713`` が実在する
 *     ＝system 由来のある orphan（要手動確認 / coid 遡及で帰属復元の対象）。
 */

export interface DelistedEntry {
  /** なぜ close 不能か（表示用の一言）。 */
  reason: string;
  /** 実判定で delisted と確認できた日付。 */
  confirmed: string;
  /** 判定の根拠（監査用）。 */
  source: string;
}

export const DELISTED_REGISTRY: Record<string, DelistedEntry> = {
  FOLD: {
    reason: '上場廃止で成行 close 不能',
    confirmed: '2026-07-30',
    source: 'alpaca asset inactive/非tradable (07-30 probe) + close 不能をユーザー確認',
  },
  CDTX: {
    reason: '上場廃止で成行 close 不能',
    confirmed: '2026-07-30',
    source: 'alpaca asset inactive/非tradable (07-30 probe) + close 不能をユーザー確認',
  },
};

/** 確定 delisted レジストリに載っている銘柄か（大小文字非依存）。 */
export function isRegistryDelisted(symbol: string | null | undefined): boolean {
  if (!symbol) return false;
  return Object.prototype.hasOwnProperty.call(
    DELISTED_REGISTRY,
    symbol.toUpperCase(),
  );
}
