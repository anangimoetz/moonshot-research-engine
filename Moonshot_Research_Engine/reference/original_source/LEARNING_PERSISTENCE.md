# Make the learning permanent — for free (GitHub as storage)

By default the scanner's learning (`.moonshot_learn.json`) lives on Render's
temporary disk, which is wiped on every redeploy/sleep. To make it **permanent
and free**, the app can commit its learning file to your GitHub repo after every
learning cycle and load it back on startup. GitHub becomes your free database.

## What you get
- The prediction journal, graded outcomes, learned factor weights, and
  per-Yerevan-session hit-rates all survive restarts.
- Every learning update is a tiny commit to `learn_state/moonshot_learn.json`,
  so you even get a full history of how the model evolved.

## One-time setup (about 5 minutes)

### 1. Create a fine-grained GitHub token
1. Go to GitHub -> Settings -> Developer settings -> **Fine-grained tokens** ->
   **Generate new token**.
2. Name: `moonshot-learning`. Expiration: your choice (90 days+ or no expiry).
3. **Repository access:** Only select repositories -> pick
   `bitget-moonshot-scanner`.
4. **Permissions:** Repository permissions -> **Contents** -> **Read and write**.
   (That is the only permission needed.)
5. Generate and copy the token (starts with `github_pat_...`).

> Security: this token can only read/write files in that one repo. It cannot
> touch your other repos or account settings. If it ever leaks, revoke it on the
> same page and make a new one.

### 2. Add the env vars in Render
Render dashboard -> your service -> **Environment** -> add:

| Key | Value |
|-----|-------|
| `MOONSHOT_GH_TOKEN` | the `github_pat_...` token |
| `MOONSHOT_GH_REPO`  | `andreluciano69/bitget-moonshot-scanner` |
| `MOONSHOT_GH_PATH`  | `learn_state/moonshot_learn.json` (default is fine) |
| `MOONSHOT_GH_BRANCH`| `main` |

Save. Render redeploys automatically. Done — learning is now permanent.

### 3. (Recommended) turn on the background scheduler too
So it keeps grading and learning even when no browser is open:

| Key | Value |
|-----|-------|
| `MOONSHOT_AUTOSCAN_MIN` | `60` (grade + scan every hour) |

## How to confirm it's working
- After a scan produces BUY-WATCH calls, check your repo: a file
  `learn_state/moonshot_learn.json` should appear/update with commits titled
  "moonshot learning update ...".
- The Self-learning panel's "Total journaled" count should persist across a
  Render redeploy instead of resetting to 0.

## Notes
- If the token is unset or wrong, the app silently falls back to the local
  (ephemeral) file — nothing breaks, learning just won't persist.
- Writes are best-effort and non-blocking; a transient GitHub hiccup never
  stops a scan.
