import type { Config } from 'tailwindcss';

const config: Config = {
  // NOTE: components/ を content に含めないと NarrativeCard.tsx で使う
  // bg-gradient-to-r / border-sky-400 / flex-wrap 等が Tailwind の JIT で
  // purge され、unstyled で render → 銘柄 chip が縦オーバーフローする崩壊
  // が起きる (2026-07-02 incident)。scan pattern を app/lib/components に拡張。
  content: [
    './app/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        card: '#1e2130',
        cardfg: '#e6e9f0',
        ok: '#4ade80',
        warn: '#facc15',
        fail: '#f87171',
        muted: '#94a3b8',
      },
    },
  },
  plugins: [],
};
export default config;
