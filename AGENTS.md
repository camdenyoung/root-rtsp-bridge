# Working on Root RTSP Bridge

These instructions record the owner's established workflow and project decisions.
Read this file at the start of a new chat or agent session. Inspect the current
checkout and remote state before acting; the handoff below is context, not proof
that branches, tags, credentials, or network access are still current.

## Repository and purpose

- GitHub: https://github.com/camdenyoung/root-rtsp-bridge
- Python RTSP transport bridge, primarily for Bosch TouchOne clients requesting
  the root URL. Each listening port selects a configured upstream camera stream.
- Relays encoded RTP/RTCP without decoding or transcoding. Upstream/downstream
  TCP and UDP are independently configurable. There is no transport fallback.
- `bridge/proxy.py`: config validation, RTSP sessions, media relay, logging, startup.
- `bridge/dashboard.py`: read-only HTTP server and allowlisted monitoring API.
- `bridge/dashboard_ui.py`: self-contained dashboard HTML/CSS/JavaScript.
- `bridge/__init__.py`: shared `__version__`, used by the dashboard status API.
- `config/config.example.yaml`: committed example, not the live user config.
- `tests/`: unittest suite using local mock cameras and real TCP/UDP sockets.

## Branch, commit, and release workflow

1. Check `git status --short`, current branch, remotes, and recent history.
   Preserve unrelated user changes. Fetch origin when access is available.
2. Implement new features/fixes on a descriptive `feat/...` or `fix/...` branch
   based on current main. Continue the same branch for related follow-up requests.
   Do not leave a requested new branch only in the local checkout.
3. Complete the change, update relevant docs, run appropriate checks, commit,
   and push the branch to origin. Established expectation: work is reviewable on
   GitHub, not merely uncommitted locally. Report the branch and validation.
4. Keep feature work off main until the owner asks to merge/release. Do not
   interpret approval of one release as permission to release future changes.
5. When explicitly asked to merge and tag a version:
   - Fetch and check the working tree and remote main; check that the tag is unused.
   - Update `bridge/__init__.py` to the requested version, without the `v` prefix.
     The dashboard must show the released version, not the previous release.
   - Commit the version change, bring local main up to date, and merge the feature
     branch with a merge commit (`--no-ff`), as used in earlier releases.
   - Run `python -m unittest discover -s tests -q` and `git diff --check`.
   - Create an annotated tag named `vX.Y.Z`. Push main and the tag (prefer an atomic
     push), plus the feature branch if its version commit is not yet pushed.
   - Confirm the push succeeded. Do not claim a GitHub release was created when
     only a Git tag was pushed. No release-page creation is required unless asked.
   - Do not force-push, overwrite existing release tags, or delete branches unless
     explicitly requested.
6. Provide short deployment commands appropriate to the actual change.

## Verification and communication

- Standard suite: `python -m unittest discover -s tests -q` (22 tests at the
  v0.2.4 handoff; the count may change). Runtime dependency: PyYAML.
- Run `git diff --check`. For UI changes, verify desktop and mobile rendering,
  refresh behavior, empty/error states, and expanded-details preservation.
- Chromium/Playwright were usable in the previous workspace, but rediscover tools
  in a new environment. Do not assume paths or installed browsers persist.
- Keep progress updates concise. Act on authorized routine work without repeatedly
  asking for confirmation. Be candid about failed commands or incomplete pushes.
- Do not spawn additional agents unless the owner or applicable instructions ask.

## GitHub access failures and resuming work

- Workspace paths, cached credentials, tool availability, and network permissions
  can change between chats. Discover the current repo rather than assuming its path.
- Use the available managed-environment skill/status tools when applicable, and
  follow the environment's network policy and injected Git authentication setup.
  Do not print credentials, environment dumps, or secret files.
- In the previous environment, Git HTTPS fetch/push worked while `gh api` against
  `api.github.com` returned Forbidden. A gh API failure alone does not establish
  that Git operations are unavailable. Try normal Git over the configured origin.
- A public GitHub HTML page was sufficient to locate the owner's `axispy` repo;
  cloning via HTTPS then worked. Do not bypass policy restrictions or disable TLS
  verification to recover access.
- If publishing is blocked, finish safe local work, retain commits, and report the
  exact branch/commit, attempted action, failure, and remaining push/merge steps.
  Never claim a local branch/commit/tag is on GitHub without a successful push.
- Before retrying after an ambiguous network failure, inspect remote refs to avoid
  duplicating or overwriting a change that already succeeded.

## Dashboard decisions

- Dashboard remains read-only: GET page and `/api/status`; no config/playback controls.
- Visual reference: https://github.com/camdenyoung/axispy, especially `axispy_ui.py`.
  The owner initially called it "asxipy"; the actual repository is `axispy`.
- Match its navy background, mint accents, muted labels, rounded cards, and badges.
  This is custom self-contained UI, without external fonts, CDNs or UI frameworks.
- Use available screen width. Routes are tabular; sessions are responsive cards
  with expandable track/SDP details. Keep details open across polling refreshes.
- Display software version from the shared version value, readable uptime/session
  age (seconds through years), per-route connection counts, requesting client
  source IP, transports, audio flag, TLS verification, and credential mode.
- Show upstream URL scheme/host/port AND path/query. The owner explicitly approved
  showing paths and queries, including stream tokens, after initially hiding them.
  Keep embedded URL usernames/passwords and configured credentials hidden.
  Do not restore blanket path/query hiding without a new request.
- Never expose auth headers, upstream RTSP session tokens, or raw SDP. Keep session
  SDP details allowlisted. Render config/protocol values using textContent, not HTML.
- Resolution is shown from advertised SDP `a=framesize`, otherwise unavailable.
  It is not inferred from decoded video; do not claim all streams report resolution.
- Connected counts all open RTSP client connections. Playing is the last successful
  RTSP PLAY state, cleared by successful PAUSE; it does not prove media delivery or
  that a client is displaying video. VLC can pause locally without sending PAUSE.
- Counters include observed RTP/RTCP in both directions, not decoded frames or
  delivery guarantees. Session counters are not historical totals.
- Dashboard is disabled in the example until enabled, with example host `0.0.0.0`
  and port 8080. Code fallback host is loopback. Port is configurable.
- No dashboard authentication/HTTPS: intended for a trusted LAN/VPN. A dashboard
  port bind failure currently prevents the bridge from starting.

## Logging, configuration, and Docker deployment

- Logs use local process time with numeric UTC offset, not a trailing UTC `Z`.
  Compose mounts `/etc/localtime:/etc/localtime:ro` to use Linux host timezone and
  daylight-saving rules. Docker's optional own timestamps are separate.
- Preserve logging sanitization: do not log passwords, auth headers, stream URLs,
  paths, query tokens, raw SDP, or raw exception strings. Dashboard URL visibility
  does not authorize exposing those values in logs.
- Compose uses Linux `network_mode: host`: no `ports:` mapping is required for
  RTSP or dashboard listeners. Host firewall rules still apply.
- Typical update on an existing branch: `git pull` then
  `docker compose up -d --build`.
- Switching to a new remote branch: `git fetch origin`, `git switch BRANCH`, then
  rebuild. Returning to released code: `git switch main`, `git pull`, rebuild.
- Config is read at process startup, without hot reload. Normally
  `docker compose restart` applies config edits; `up -d` alone may not restart.
  The owner reported using `--force-recreate`; do not assert their deployment was
  diagnosed. Mount/Compose changes need recreation, for example:
  `docker compose up -d --build --force-recreate`.
- Live `config/config.yaml`, `.env`, logs and certificates are private/ignored.
  Do not commit them. Updating the example does not update existing live config.
- If checkout is blocked by local edits, preserve them by default. The owner once
  explicitly authorized discarding `compose.yaml` edits using `git restore`;
  that was specific to that incident, not standing permission to discard work.

## TouchOne observations and open limitations

- With `audio_enabled: false`, closing the TouchOne view sends TEARDOWN and closes
  the session correctly. This is the confirmed workaround for lingering audio.
- With audio enabled, closing the view stops visible video but audio continues;
  there are no corresponding requests/disconnect entries even with DEBUG enabled.
  Reopening creates another session, causing accumulation.
- Evidence suggests TouchOne keeps its audio-enabled session open. This is an
  observation, not a fully diagnosed firmware defect. The bridge has no confirmed
  signal that the view closed; do not invent a reliable close detector.
- A per-client-IP/per-route session limit was discussed ONLY as a possible
  workaround. It has NOT been implemented or requested. It would close the older
  session on reopening, not stop lingering audio immediately when the view closes,
  and may affect legitimate clients behind a shared IP.
- Downstream TCP is enforced; there is no fallback to UDP. A successful TCP track
  setup and actual playback demonstrate client TCP support; a mere RTSP connection
  does not. Logs show `event=track_setup downstream=tcp` when negotiated.

## Release handoff (recorded 2026-10-30)

- Latest completed release at this handoff: v0.2.4, main commit `469833d`.
- v0.2.2: read-only dashboard; v0.2.3: axispy-style UI and displayed version;
  v0.2.4: local log timestamps and host timezone mount.
- Those releases and their feature branches were successfully pushed to GitHub.
- Recheck actual history/version/tags before future releases; this section is a
  dated handoff, not a requirement to keep releasing from this version forever.
- This AGENTS.md addition is documentation, not a request for another release tag.
