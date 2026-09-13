# Frontend development

[← back to README](../README.md)

Only needed if you are changing the UI. `npm run dev` serves it with hot reload
on :5173 and proxies `/api` to the Python backend, so run both:

```bash
npm --prefix web install       # once
./lemondx serve --dev          # terminal 1
npm --prefix web run dev       # terminal 2 → http://localhost:5173
```

Point the proxy somewhere else with `LEMONDX_API=http://host:port npm run dev`.

`web/dist` is committed so clones run without Node. After changing anything
under `web/src`, rebuild and commit the result:

```bash
npm --prefix web run build
```
