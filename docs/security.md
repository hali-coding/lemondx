# Security

[← back to README](../README.md)

`serve` binds to `127.0.0.1` by default. The API can create, modify and delete
containers, so anyone who can reach the port controls your containers — there
is no per-user authorization beyond the token.

If you bind it to a routable address, set a token:

```bash
./lemondx serve --host 0.0.0.0 --port 8099 --token "$(openssl rand -hex 24)"
```

Clients then send `Authorization: Bearer <token>` (or `X-Lemondx-Token`). The
web UI prompts for the token the first time the API answers `401` and keeps it
in `sessionStorage` for that tab only.

Serving over plain HTTP sends that token in the clear; put it behind a
TLS-terminating proxy if it leaves the machine. The server warns when it binds
to a non-loopback address without a token. The static UI itself is served
without authentication — only `/api/*` is gated.
