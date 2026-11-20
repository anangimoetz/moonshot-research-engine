# Deploy the Moonshot Scanner to Render (free, permanent URL)

Render gives you a permanent `https://<name>.onrender.com` URL that stays online
24/7 (your PC can be off). On the **free** plan the service sleeps after ~15 min
of no traffic and wakes on the next visit (first load takes ~30–60s, then it's
fast again). The URL never changes.

The repo already contains everything Render needs:
- `render.yaml`   – service blueprint (build + start command)
- `Procfile`      – start command fallback
- `runtime.txt`   – Python 3.11.9
- `requirements_moonshot.txt` – deps (flask, waitress, pandas, numpy, requests)

## One-time setup

### 1. Put the code on GitHub
Render deploys from a Git repo. From this folder:

```bash
git init
git add .
git commit -m "Bitget moonshot scanner + web dashboard"
```

Create an empty repo on github.com, then:

```bash
git remote add origin https://github.com/<you>/bitget-moonshot-scanner.git
git branch -M main
git push -u origin main
```

### 2. Create the Render service
1. Go to https://render.com and sign up (free, GitHub login works).
2. Click **New +** → **Web Service**.
3. Connect your GitHub and pick the repo.
4. Render detects `render.yaml` automatically. If it asks manually, use:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements_moonshot.txt`
   - **Start command:** `waitress-serve --host=0.0.0.0 --port=$PORT moonshot_web:app`
   - **Plan:** Free
5. Under **Environment**, add a variable:
   - `MOONSHOT_TOKEN` = a long random secret (e.g. a 20+ char string)
   This locks the API so only you can run scans.
6. Click **Create Web Service**. First build takes a few minutes.

### 3. Open it
Render shows a URL like `https://bitget-moonshot-scanner.onrender.com`.
Open it with your token:

```
https://bitget-moonshot-scanner.onrender.com/?token=YOUR_SECRET
```

The page stores the token in your browser, so later you can just open the base
URL on that device.

## Updating later
Push to `main` and Render auto-redeploys:

```bash
git add -A && git commit -m "tweak" && git push
```

## Notes / limits
- **Free tier sleep:** first request after idle is slow (cold start), then normal.
- **Ephemeral disk:** Render wipes files on restart. That only affects the local
  OI history file (`.moonshot_oi_state.json`) and the CoinGecko cache — both are
  rebuilt automatically. OI-delta simply resets after a sleep/redeploy.
- **Keep the token on.** A public URL means anyone with it can trigger scans that
  call Bitget/CoinGecko from the server. The token prevents that.
- **Rate limits:** see the "How many scans" section in the main README.
