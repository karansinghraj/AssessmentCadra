# Deploy on Vercel (serverless)

The service is a normal ASGI FastAPI app, but a permanent server config (uvicorn plus a repo-local
SQLite file) does not fit Vercel: the deployment filesystem is read-only apart from `/tmp`, and
functions are created and frozen per instance.

This repository includes a serverless setup:

| File | Purpose |
|---|---|
| `main.py` | Vercel entrypoint; exports the FastAPI `app` from `src/app.py` |
| `vercel.json` | Python function duration limit and bundle exclusions |
| `.python-version` | pins Python 3.12 |
| `.vercelignore` | keeps tests/eval/scripts/source tickets out of the upload |
| `src/config.py` | when `VERCEL=1`, defaults write paths to the platform temp directory |

## 1. What changes on Vercel

Vercel sets `VERCEL=1`. `Config.from_env()` then uses:

| Setting | Local default | Vercel default |
|---|---|---|
| `RUN_DB_PATH` | `var/runs.db` | `/tmp/larkfield-runs.db` |
| `DATA_BUILD_DIR` | `build/` | `/tmp/larkfield-build` |
| `DATA_DIR` | `data/` | `data/` (read-only, read normally) |

`ensure_built()` writes the prepared `data.json` and manifest into `/tmp/larkfield-build`; SQLite
writes its database, WAL and SHM files into `/tmp`. Nothing is written into the repo.

`main.py` exists because Vercel looks for a top-level `app` in `main.py` (or `app.py`, `index.py`,
`server.py`, `wsgi.py`, `asgi.py`). It imports the same app used by local uvicorn, so there is no
second implementation.

## 2. Deploy

### Dashboard

1. Push the repository to GitHub/GitLab/Bitbucket.
2. In Vercel, **Add New → Project** and import the repo.
3. Framework preset: **FastAPI** (auto-detected from `requirements.txt`). Root directory: repository
   root.
4. Add environment variables:

   | Name | Value |
   |---|---|
   | `LLM_API_KEY` | your provider key (secret) |
   | `LLM_BASE_URL` | `https://api.openai.com/v1` (or your compatible endpoint) |
   | `LLM_MODEL` | e.g. `gpt-4o-mini` |
   | `LLM_INPUT_USD_PER_MTOK` | `0.15` |
   | `LLM_OUTPUT_USD_PER_MTOK` | `0.60` |
   | `LOG_LEVEL` | `INFO` |

   Do **not** set `RUN_DB_PATH` or `DATA_BUILD_DIR` to repo paths. Leave them unset (or point them
   at `/tmp/...`) so the serverless defaults apply.
5. Deploy. The health check is `GET /health`.

### CLI

```bash
npm i -g vercel
vercel login
vercel --prod
```

Set the same environment variables in **Project → Settings → Environment Variables**, or with
`vercel env add LLM_API_KEY production`.

## 3. Verify

```bash
curl https://<project>.vercel.app/health
# {"status":"ok"}

curl https://<project>.vercel.app/openapi.json
# Swagger schema

curl -X POST https://<project>.vercel.app/tickets \
  -H "Content-Type: application/json" \
  --data @inbox/public_tickets.json
```

Or open `https://<project>.vercel.app/docs` for Swagger UI.

Locally, without Vercel:

```bash
python -m scripts.vercel_smoke
# 10/10 checks passed; default and runtime db/build paths printed under the OS temp directory
```

The smoke test first asserts the real serverless defaults, then switches to a fresh temp directory
so it can be rerun safely: `/tmp` survives warm invocations, and the default database may already
hold a decided ticket from an earlier run.

## 4. Important limitation: `/tmp` is not durable

`/tmp` is writable but **per instance and ephemeral**:

- A cold start or redeploy gets a fresh `/tmp`, so runs, decisions and the ledger are lost.
- Two concurrent function instances do not share the same SQLite file, so idempotency is only
  guaranteed within one instance.
- This is acceptable for the assessment/demo and for temporary single-instance runs, not for a
  production ledger.

For durable, shared state, replace `src/store.py` with Postgres (or another managed database) behind
the same methods. The unique keys (`runs.ticket_id`, `decisions.run_id`, `ledger(run_id, action)`)
are the contract to preserve.

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `OSError: [Errno 30] Read-only file system` | An env var points at `var/`, `build/` or another repo path. Leave `RUN_DB_PATH` / `DATA_BUILD_DIR` unset or use `/tmp/...`. |
| `ModuleNotFoundError: src` | Ensure `main.py` is at the repo root and the project root directory is the repository root in Vercel. |
| 404 on every route | Vercel did not detect the FastAPI entrypoint. Confirm `main.py` exports `app` and `requirements.txt` contains `fastapi`; redeploy. |
| 504 / timeout | `vercel.json` sets `maxDuration: 60`. A model call uses an 8 s timeout; if your provider is slower, lower `LLM_TIMEOUT_SECONDS` or disable the judge with `LLM_JUDGE_ENABLED=false`. |
| Cold start is slower | The first invocation after a deploy prepares `data.json` under `/tmp`; later warm invocations reuse it. |
| Runs disappear | Expected with `/tmp`; move to Postgres for durable state. |

## 6. Files that are not used by Vercel

`Dockerfile` and `render.yaml` remain for a container/Render deployment. They do not affect the
Vercel build. The Vercel entrypoint is `main.py`; local uvicorn still uses `src.app:app`.
