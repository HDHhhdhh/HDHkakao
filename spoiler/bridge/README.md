# Spoiler read-only KakaoTalk bridge

This directory contains the network-facing companion process for Spoiler. It
uses a separately registered tablet session and the SDK's push listener; it
does not inspect the PC KakaoTalk files or windows. The bridge accepts only a
credential object supplied through stdin, emits newline-delimited JSON on
stdout, and never writes credentials to disk.

## Build and tests

```powershell
npm install --cache ..\.npm-cache --ignore-scripts --no-audit --no-fund
npm test
npm run build
```

Dependencies are pinned to `agent-messenger@2.37.1`
(`db084a7a89ba69c549802f32b83e898b5da3526f`) and `esbuild@0.28.2`.

## Runtime bridge

The host must DPAPI-decrypt its credential handoff in memory and keep the
stdin pipe open while the bridge runs:

```powershell
'{"oauthToken":"…","refreshToken":"…","userId":"…","deviceUuid":"…","deviceType":"tablet"}' | node dist\spoiler-bridge.mjs
```

The process emits `status` events (`starting`, `connected`, `ready`, and so
on) and normalized `message` events. Only read-side listener events are
subscribed. On `SIGINT`, `SIGTERM`, or stdin end, the listener and LOCO socket
are closed before the process exits.

If an expired access token is refreshed, the bridge emits a `credentials`
event and waits for `{"type":"credentials_saved"}` from the host before it
reuses the rotated token. Spoiler sends that acknowledgement only after its
atomic CurrentUser-DPAPI save succeeds.

## Tablet registration

The separate auth entry accepts a registration request line with
`{"email":"…","password":"…","deviceType":"tablet","force":false}`.
The first attempt always probes safely with force disabled. If the tablet slot
is occupied it emits a `status` event with `status:"needs_force"` and exits;
the host must ask the user and launch a second request with `force:true` (and
the same `deviceUuid`, when one was supplied in the first response):

```powershell
'{"email":"…","password":"…","deviceType":"tablet","force":false}' | node dist\spoiler-auth.mjs
# after the user confirms replacement:
'{"email":"…","password":"…","deviceType":"tablet","force":true,"deviceUuid":"…"}' | node dist\spoiler-auth.mjs
```

It emits a `device` event before any network request, a `passcode` event when
Kakao supplies one, and then a nested-plus-flat `credentials` event on success
or a structured `error` event. The password is held only in process memory for
the duration of the login flow; no credential manager or file API is used.
`force:true` may replace the existing tablet session, so the host should run
that second entry only after the user confirms it.

## Reproducible runtime inputs

Builds require Node 24.19.0 (the SHA256 in `../tools/runtime-lock.json`),
agent-messenger 2.37.1 and esbuild 0.28.2 from the committed package lock.
Use `../build.ps1 -RestoreDependencies` to restore only this checkout's
locked dependencies; install scripts are disabled. Junction dependency
folders are rejected. Each normal build bundles the current bridge and auth
sources, records input hashes in `build/build-provenance.json`, and verifies
the finished executable's embedded resources in a test-owned cache.

Spoiler publishes `active-runtime.manifest` in its private runtime root only
after verifying all three extracted payloads. KakaoSender follows that
manifest and verifies the complete runtime instead of choosing a cache by
modification time. Restart the updated Spoiler once to publish the runtime.
The runtime payloads are content addressed; .NET Framework executable bytes
may differ between compiler runs, so the executable hash is recorded rather
than claiming binary-identical PE output.

## Empty direct-room metadata

`src/room-names.mjs` shares `../../bridge/kakao_direct_metadata.mjs` with
KakaoSender. A `CHATINFO` body status of `-401` can occur for an empty direct
room. This case uses two consistent `CHATONROOM` / `GETMEM` reads to prove
the room and peer IDs before looking up the saved friend name. Other failures
do not select this fallback. It sends no messages or read receipts.

The shared module is bundled into the executable and included in build
provenance. Keep the repository layout when building Spoiler from source.
