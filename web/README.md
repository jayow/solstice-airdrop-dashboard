# Solstice × Exponent — airdrop activity UI

Static Next.js site showing every Solstice airdrop registrant and their Exponent YT/LP
on-chain activity. Fully static; no backend needed.

## Regenerate data

From the repo root:

```bash
python3 src/build_web_data.py     # refreshes web/public/data.json + events/*.json
```

## Develop

```bash
cd web
npm install
npm run dev   # http://localhost:3000
```

## Deploy to Vercel

```bash
cd web
vercel login          # browser-based login (once)
vercel                # first deploy — accept defaults
vercel --prod         # push a production deploy
```

Vercel auto-detects the Next.js framework; `vercel.json` pins `out` as the output dir.
No env vars required — all data is baked into the static build.
