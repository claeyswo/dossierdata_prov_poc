# Troubleshooting

## Prerequisites

**PostgreSQL 16 or newer must be running on `127.0.0.1:5432`** with a `dossiers` database owned by a `dossier` role. SQLite is not supported — the engine uses native `UUID` and `JSONB` columns. See README's "Prerequisites" section for the apt + psql setup commands.

You can verify the database is reachable with:

```bash
psql -h 127.0.0.1 -U dossier -d dossiers -c "SELECT 1"
```

If that returns `1` you're good. If it asks for a password, `pg_hba.conf` isn't configured for trust auth on localhost — either set a password in the connection string (`postgresql+asyncpg://dossier:dossier@...`) or change the `host all all 127.0.0.1/32` line in `/etc/postgresql/16/main/pg_hba.conf` to `trust` and reload (`pg_ctlcluster 16 main reload`). Trust auth is fine for localhost dev; in production use a real password or client certificates.

All five projects must be installed in editable mode before launching anything:

```bash
pip install -e dossier_common/ -e file_service/ -e dossier_engine/ \
            -e dossier_toelatingen/ -e dossier_app/
```

On a fresh Debian/Ubuntu environment with a system Python, you'll need `--break-system-packages` on each install (PEP 668). The alternative is a virtualenv:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e dossier_common/ -e file_service/ -e dossier_engine/ \
            -e dossier_toelatingen/ -e dossier_app/
```

The file service needs `python-multipart` for form uploads. It's in `file_service`'s `pyproject.toml` deps, so the install above pulls it in automatically. If you see `RuntimeError: Form data requires "python-multipart"` in `/tmp/files.log` at launch time, your install didn't complete — rerun the editable install for `file_service/`.

## The namespace-package collision

If you launch `uvicorn dossier_app.main:app` from the repo root and get:

```
ImportError: cannot import name 'create_app' from 'dossier_engine' (unknown location)
```

...the problem is that cwd is earlier on `sys.path` than your pip site-packages, and Python found the outer `dossier_engine/` project directory as a PEP 420 namespace package before resolving the real pip-installed package one level down. The namespace package has no `__init__.py` at that level, so the import fails with "unknown location."

**Fix**: launch uvicorn from a cwd that isn't the repo root. `/tmp` works:

```bash
cd /tmp
uvicorn dossier_app.main:app --port 8000
```

The `file_service.storage_root` path in `config.yaml` is resolved against the **config file's own directory**, not cwd, so launching from `/tmp` still lands the file storage in the right place (inside `dossier_app/dossier_app/file_storage/`). The database is Postgres, so its location is the `database.url` and isn't affected by cwd at all.

## Wiping the database between test runs

The test suite is order-dependent on fresh state. To wipe Postgres and rerun cleanly:

```bash
psql -h 127.0.0.1 -U dossier dossiers \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO dossier;"
rm -rf /home/claude/toelatingen/dossier_app/dossier_app/file_storage
```

The `DROP SCHEMA public CASCADE` drops every table the engine created. The next launch's `await create_tables()` recreates them empty. The file storage wipe is a separate step because uploaded files live on the filesystem under the dossier_app package directory, not in Postgres.

**Always `pkill` uvicorn before wiping**, and confirm `ps -ef | grep uvicorn` is clean before relaunching. The dossier API holds Postgres connections via SQLAlchemy's connection pool, and dropping the schema while live connections are using it can leave SQLAlchemy with stale prepared statements that error out on the next request. Killing the API first releases the connections cleanly.

## Running inside an agentic tool wrapper

If you're running the test suite inside a tool that wraps bash cells with a timeout and kills the cell's process group on return (Claude Code, notebook runners, etc.), don't bundle the `setsid` launches, the `sleep 4`, and the `curl` liveness check into a single cell. Split them into three:

1. **Kill + wipe**:
   ```bash
   pkill -9 -f uvicorn
   sleep 1
   ps -ef | grep uvicorn | grep -v grep || echo clean
   psql -h 127.0.0.1 -U dossier dossiers \
     -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO dossier;"
   rm -rf /home/claude/toelatingen/dossier_app/dossier_app/file_storage
   ```

2. **Launch** (detached via `setsid` + `</dev/null` to survive cell exit, and cwd set to `/tmp` to dodge the namespace-package collision):
   ```bash
   cd /tmp
   setsid python3 -m uvicorn dossier_app.main:app --port 8000 \
     </dev/null >/tmp/dossier.log 2>&1 &
   setsid python3 -m uvicorn file_service.app:app --port 8001 \
     </dev/null >/tmp/files.log 2>&1 &
   echo launched
   ```

3. **Verify + run**:
   ```bash
   sleep 4
   curl -s -o /dev/null -w "dossier:%{http_code}\n" http://localhost:8000/dossiers
   curl -s -o /dev/null -w "files:%{http_code}\n"   http://localhost:8001/health
   bash /home/claude/toelatingen/test_requests.sh > /tmp/test_run.log 2>&1
   grep -c "OK:" /tmp/test_run.log
   ```

Even with `</dev/null` and `setsid`, some wrappers hang waiting on backgrounded children to exit before returning, and the cell times out. When that happens the tool kills the cell's process group and takes the services down with it.

## Liveness signals

| Endpoint | Expected | Meaning |
|---|---|---|
| `GET http://localhost:8000/dossiers` | 401 | Dossier API alive, auth middleware wired |
| `GET http://localhost:8001/health` | 200 | File service alive |
| `psql -h 127.0.0.1 -U dossier -d dossiers -c "SELECT 1"` | `1` | Postgres reachable |

Anything returning 000 (curl's "couldn't connect") means that service isn't actually listening. Check `/tmp/dossier.log` or `/tmp/files.log` for the crash reason before proceeding. The dossier API has no `/health` endpoint, so 401 on `/dossiers` is the canonical liveness signal — it proves auth middleware is up.

## Test ordering and fresh state

The test suite is order-dependent on fresh state (fixed entity/activity UUIDs per dossier). Running a subset of tests, or re-running the suite against a dirty DB, will produce mixed results: some assertions pass because the expected state is there from the previous run, others fail on replay detection. Always wipe the database between full runs.

## Worker operations

The worker runs as a separate process. It polls Postgres for due task entities, executes them inside a row-locked transaction, and writes completion/retry/dead-letter state via the engine's `systemAction` activity path. Every write goes through `execute_activity` — the worker has zero direct row writes.

### Running the worker

```bash
# Drain everything currently due and exit
python -m dossier_engine.worker --once

# Continuous polling (default 10s interval)
python -m dossier_engine.worker

# Faster polling
python -m dossier_engine.worker --interval 2

# Help
python -m dossier_engine.worker --help
```

Launch cwd doesn't matter — the worker discovers `config.yaml` via the installed `dossier_app` package. Override with `--config /path/to/config.yaml` if needed.

### Running multiple workers safely

Postgres `SELECT ... FOR UPDATE OF entities SKIP LOCKED` makes multi-worker deployments safe with zero additional coordination. Run as many worker processes as your throughput requires — each claim lock is per-row and released on transaction commit, so workers never race on the same task version.

A sensible starting point is one worker per CPU core for CPU-bound task functions, or a few per core for IO-bound functions. All workers connect to the same Postgres instance. No leader election, no lease store, no Redis — Postgres handles it.

### Dead-letter inspection and requeue

A task that fails `max_attempts` times is written with `status = "dead_letter"` and stays in that state forever unless an operator intervenes. The worker emits an ERROR-level log line (`Task X: attempt K/M failed, moving to dead_letter`) with `exc_info` and a structured `extra` dict — deployments wired to Sentry will see this as a full Sentry event with stack trace and task/dossier/attempt tags.

Error telemetry (exception type, message, stack trace, breadcrumbs) lives in **Sentry**, not in the database. The task's content only carries operational state that the worker needs for retry decisions (`attempt_count`, `last_attempt_at`, `next_attempt_at`). To investigate why a task died, query Sentry by `task_id:<entity_id>` and read the full attempt history there.

To see which tasks are currently dead-lettered:

```sql
WITH latest AS (
  SELECT DISTINCT ON (entity_id) *
  FROM entities
  WHERE type = 'system:task'
  ORDER BY entity_id, created_at DESC
)
SELECT entity_id, dossier_id,
       content->>'function'        AS fn,
       content->>'attempt_count'   AS attempts,
       content->>'last_attempt_at' AS last_tried
FROM latest
WHERE content->>'status' = 'dead_letter'
ORDER BY content->>'last_attempt_at' DESC;
```

Once the root cause is fixed, requeue dead-lettered tasks via the `--requeue-dead-letters` CLI flag:

```bash
# Everything across all dossiers
python -m dossier_engine.worker --requeue-dead-letters

# Only dead letters in one dossier
python -m dossier_engine.worker --requeue-dead-letters --dossier=<uuid>

# One specific task by its logical entity_id
python -m dossier_engine.worker --requeue-dead-letters --task=<entity_uuid>
```

The command resets each matching task's `status` to `scheduled`, zeroes `attempt_count`, clears `next_attempt_at`, and preserves the original `scheduled_for` and `last_attempt_at` for historical context. Each dossier's requeue is written as one `systemAction` activity generating N task revisions + one `system:note` describing the scope, so the requeue is fully auditable in the PROV graph. Running the command with 50 dead letters spread across 10 dossiers produces 10 audit entries (one per dossier), each listing the affected tasks.

The requeue command runs as a one-shot and exits. To immediately execute the requeued tasks, run `python -m dossier_engine.worker --once` afterward or wait for the next normal poll cycle.

### Graceful shutdown

The worker installs handlers for `SIGTERM` and `SIGINT`. Signal delivery sets an `asyncio.Event` that the poll loop watches; the interruptible sleep (`asyncio.wait_for(shutdown.wait(), timeout=poll_interval)`) returns immediately, the outer loop exits, and the worker logs `Worker stopped` and quits.

A signal arriving mid-drain finishes the in-flight task cleanly — its transaction runs to completion, the row lock is released, the completion is committed — and then the inner drain loop's top-of-iteration check breaks out before starting the next task. **Tasks are never interrupted mid-transaction.** The shutdown latency bound is roughly one task's execution time plus the remaining sleep slice (typically sub-second in practice).

Container orchestrators (Kubernetes, systemd) should send SIGTERM and wait at least a few seconds before SIGKILL. The longest in-flight task in your workload is the lower bound on the grace period.

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Worker log shows `database.url is required in config` at startup | Config file missing `database.url` or Postgres is down | Verify `config.yaml` has the Postgres URL; `pg_lsclusters` to confirm Postgres is running |
| `Task X: attempt K/M failed, retry at T` appearing repeatedly for the same task | Task function is deterministically failing | Pull the task's error history from Sentry (query by `task_id:X`), fix the underlying cause, wait for the task to dead-letter or requeue it manually |
| `Task X: attempt K/M failed, moving to dead_letter` | Task hit `max_attempts` | Pull error history from Sentry, fix root cause, `python -m dossier_engine.worker --requeue-dead-letters --task=<entity_uuid>` |
| Worker drains nothing even though there are `scheduled` tasks | `next_attempt_at` is set to a future time (task is waiting on retry delay) OR `scheduled_for` hasn't arrived yet OR another worker has the row locked | Check `content->>'next_attempt_at'` and `content->>'scheduled_for'` on the latest task version; both must be ≤ now for it to be claimable |
| Multiple workers each pick up different-but-overlapping subsets of tasks | Expected — `FOR UPDATE OF entities SKIP LOCKED` distributes tasks across workers per-row | No fix needed, this is correct concurrent operation |
| After requeue, task hits 422 "Invalid derivation chain" | `_refetch_task` bug, fixed | Ensure your worker.py uses `get_latest_entity_by_id` in `_refetch_task`, not the old `get_entities_by_type` loop |

### Postgres cluster going down

On development sandboxes, the Postgres cluster may go down between restarts. Symptom: any psql or worker command fails with `connection to server at "127.0.0.1", port 5432 failed: Connection refused`. Fix:

```bash
pg_ctlcluster 16 main start
pg_lsclusters           # should show "online"
```

If the cluster shows a stale pid file, `pg_ctlcluster start` removes it automatically and starts fresh.
