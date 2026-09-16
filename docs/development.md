# Development

[← back to README](../README.md)

## Frontend

Only needed if you are changing the UI. `npm run dev` serves it with hot reload
on :5173 and proxies `/api` to the Python backend, so run both:

```bash
./build.sh                     # once: checks Node, installs, builds
./lemondx serve --dev          # terminal 1
npm --prefix web run dev       # terminal 2 → http://localhost:5173
```

Point the proxy somewhere else with `LEMONDX_API=http://host:port npm run dev`.

`web/dist` is a build output and is **not** in git — it is rebuilt by the
release workflow and shipped inside the release archive. Build it once in a
clone, and again after changing anything under `web/src`:

```bash
./build.sh            # refuses early if Node is missing or too old
./build.sh --clean    # throw away web/dist and node_modules first
```

`./build.sh` is what CI runs too, so a clone and a release are built the same
way. Under it is just `npm --prefix web run build`, if you would rather drive
that directly. There is nothing to commit afterwards: `.gitignore` covers
`web/dist`.

## Releases

Releases are cut by [release-please](https://github.com/googleapis/release-please)
from the commit history, so the version number is decided by what was merged,
not by anyone editing a file:

| Commit subject | Effect on `0.4.2` |
| --- | --- |
| `fix: ...`, `perf: ...`, `refactor: ...` | `0.4.3` |
| `feat: ...` | `0.5.0` |
| `feat!: ...`, or a `BREAKING CHANGE:` footer | `0.5.0` while the project is pre-1.0, `1.0.0` and up after |
| `docs: ...`, `chore: ...`, `ci: ...` | none |

The PR workflow rejects a PR title that is not a conventional commit, because a
squash merge turns that title into the commit subject — the whole input to the
version number above.

The flow, all in `.github/workflows/release.yml`:

1. A merge to `main` makes release-please open (or update) a release PR titled
   `chore(main): release <version>`. It holds the `CHANGELOG.md` entry and the
   bumped version in `pyproject.toml` and `src/lemondx/__init__.py`.
2. Merging that PR is the decision to release. release-please tags
   `v<version>` and creates the GitHub release.
3. The same run then does `npm ci && npm run build` and attaches
   `lemondx-<version>.tar.gz`, `.zip` and `SHA256SUMS`.

The archive is the runnable tree — launcher, `src/`, the built `web/dist`,
`modules/`, `systemd/` and the docs — so a user needs only Python 3.9+. Build
the same thing locally to see what a release will contain:

```bash
./build.sh
.github/scripts/bundle.sh 1.2.3     # -> dist/lemondx-1.2.3.{tar.gz,zip}
```

To force a version — the first `1.0.0`, say — put `Release-As: 1.0.0` in a
commit body, or add it to a commit on the release PR.

Two repository settings this depends on: **Allow GitHub Actions to create and
approve pull requests** (Settings → Actions → General), or release-please
cannot open its PR; and squash merging enabled, so PR titles are what lands on
`main`.

### The `RELEASE_PLEASE_TOKEN` secret

GitHub will not let one workflow run trigger another, so a release PR opened by
`github-actions[bot]` cannot start the PR workflow: the run is created and dies
immediately with *Actor is not allowed to trigger Actions workflows*. The
effect is that the one PR nobody wrote by hand is the one that goes unchecked.

Giving release-please a token that belongs to an account fixes it — the PR is
opened by that account, and CI treats it like any other. Create a
[fine-grained PAT](https://github.com/settings/personal-access-tokens) scoped to
this repository with **Contents: read and write** and **Pull requests: read and
write**, then store it as the repository secret `RELEASE_PLEASE_TOKEN`
(Settings → Secrets and variables → Actions).

It is optional: without the secret the workflow falls back to `GITHUB_TOKEN`,
which still tags and releases correctly — only the checks on the release PR are
lost, and they show as a startup failure rather than as nothing. Nothing else in
the repository needs the token, and a fine-grained PAT expires, so the red X
coming back on a release PR is the sign that it needs renewing.
