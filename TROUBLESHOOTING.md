# Troubleshooting

## Prerequisites

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

Config files and relative paths inside them (`database.url`, `file_service.storage_root`) are resolved against the **config file's own directory**, not cwd, so launching from `/tmp` still lands the database and file storage in the right place (inside `dossier_app/dossier_app/`). Same rule for the file service — it discovers its config via the installed `dossier_app` package location.

## The deleted-inode gotcha

If you `rm` the database while a uvicorn process is still running, the server holds the unlinked inode via an open fd and keeps serving the old state, while a new empty `dossiers.db` file appears on disk. Subsequent test runs then see an impossible mix of "empty DB on disk" and "fully-populated state via the API," with frozen timestamps and replayed idempotency responses.

Always `pkill` **before** `rm`, and confirm `ps -ef | grep uvicorn` is clean before restarting. You can verify the live process is pointing at the current file (not a deleted one) with:

```bash
PID=$(pgrep -f "dossier_app.main:app")
ls -la /proc/$PID/fd/ | grep "\.db"
# Bad:  .../dossiers.db (deleted)
# Good: .../dossiers.db
```

## Running inside an agentic tool wrapper

If you're running the test suite inside a tool that wraps bash cells with a timeout and kills the cell's process group on return (Claude Code, notebook runners, etc.), don't bundle the `setsid` launches, the `sleep 4`, and the `curl` liveness check into a single cell. Split them into three:

1. **Kill + wipe**:
   ```bash
   pkill -9 -f uvicorn
   sleep 1
   ps -ef | grep uvicorn | grep -v grep || echo clean
   rm -f  /home/claude/toelatingen/dossier_app/dossier_app/dossiers.db*
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

Even with `</dev/null` and `setsid`, some wrappers hang waiting on backgrounded children to exit before returning, and the cell times out. When that happens the tool kills the cell's process group and takes the services down with it, leaving you with the deleted-inode scenario described above.

## Liveness signals

| Endpoint | Expected | Meaning |
|---|---|---|
| `GET http://localhost:8000/dossiers` | 401 | Dossier API alive, auth middleware wired |
| `GET http://localhost:8001/health` | 200 | File service alive |

Anything returning 000 (curl's "couldn't connect") means that service isn't actually listening. Check `/tmp/dossier.log` or `/tmp/files.log` for the crash reason before proceeding. The dossier API has no `/health` endpoint, so 401 on `/dossiers` is the canonical liveness signal — it proves auth middleware is up.

## Test ordering and fresh state

The test suite is order-dependent on fresh state (fixed entity/activity UUIDs per dossier). Running a subset of tests, or re-running the suite against a dirty DB, will produce mixed results: some assertions pass because the expected state is there from the previous run, others fail on replay detection. Always wipe the database between full runs.
