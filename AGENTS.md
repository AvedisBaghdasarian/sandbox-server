# Repository instructions

This repository contains the standalone OpenHands FastAPI application and
sandbox control plane. The primary code lives in `openhands/app_server/`.

Before changing code, run:

```bash
make install-pre-commit-hooks
```

Before pushing changes:

```bash
make lint
make test
```

Use specific paths with `git add`. Keep the server runnable through
`openhands.app_server.app:app`; do not reintroduce a dependency on the removed
monorepo frontend or `openhands.server.listen`.

Third-party GitHub Actions must be pinned to a full commit SHA. Preserve the
tool versions recorded in `poetry.lock` and `uv.lock` when regenerating either
lockfile.
