# SiteFilter Native Messaging Host

This directory contains the allowlisted Windows bridge between the SiteFilter
extension and Universal Web Collector. The host uses Native Messaging protocol
version 1 and reserves stdout exclusively for four-byte little-endian framed
JSON responses. Diagnostics go to stderr and rotating files under
`%LOCALAPPDATA%\SiteFilter\NativeHost`.

The installer (Task 6) owns `config.json` at that fixed location. Its required
fields are `protocol_version`, `allowed_origins`, `distro`, `project_path`, and
`runtime_file`; `startup_timeout_seconds` is the only optional field. No other
keys are accepted. `protocol_version` is the integer `1`, `allowed_origins`
contains exactly one `chrome-extension://<32 lowercase a-p characters>/`
origin, `distro` is a bounded WSL distribution name, `project_path` is a
bounded absolute Linux path without controls, backslashes, or `..`, and
`runtime_file` is a bounded absolute Windows path without controls or `..`.
The optional startup timeout is a number from 1 through 120 seconds. Browser
requests cannot override any of these values. The manifest
template placeholders are also replaced by the installer with the packaged
host path and the exact stable SiteFilter extension origin.

## Allowlisted operations

| Action | Collector operation |
| --- | --- |
| `ping` | `GET /healthz` after Collector-specific health/config validation |
| `preview` | `POST /tasks/preview` with a 150-second response timeout |
| `create-or-reuse` | `POST /tasks/create` with deduplication and incremental reuse enabled |
| `get-task` | `GET /tasks/<positive integer>` |
| `open-task` | Open `http://127.0.0.1:<descriptor-port>/?task=<positive integer>` |
| `start-login` | `POST /sessions/login` |

Only exact HTTPS `xchina.co` photo/video detail URL shapes are accepted. Query
and fragment variants are accepted as the same approved page location, but the
host strips both before forwarding the URL to Collector. Collector still owns
canonical content identity. The protocol rejects unknown fields, arbitrary
endpoints, commands, executable paths, WSL distributions, project paths,
ports, and shell fragments.

If no healthy descriptor is available, the host invokes this fixed argument
array with `shell=False`:

```text
wsl.exe -d <configured-distro> --cd <configured-project-path> -- make start-native
```

Collector remains the owner of URL resolution, canonical identity, duplicate
decisions, login state, task state, downloads, and output settings.

Ordinary proxied API requests use a 15-second timeout. Preview alone uses 150
seconds because the existing collector may make two attempts (anonymous and
saved state), each with up to 30 seconds of navigation plus a 25-second wait.
That valid path consumes a 110-second base budget before fallback work; the
remaining 40 seconds provides bounded processing and scheduling margin.
Startup retains its separately bounded 60-second readiness window.

Host discovery is intentionally stricter than the generic Task 4 supervisor
probe: in addition to `/healthz`, `/config` must report `auto_collector` as
`auto`, both `xchina_gallery` and `xchina_video`, and the `image` and `video`
resource types. This prevents an unrelated service with similarly shaped
health/config responses from being trusted by the bridge without changing the
shared Task 4 discovery contract.

All local HTTP calls refuse redirects and require the final URL to remain the
exact fixed loopback URL. Discovery and action response bodies are capped at 1
MiB before strict JSON parsing; runtime descriptors use the same file-size
bound. JSON nesting is capped at 256 levels, and `NaN` and infinities are
rejected. Unexpected host failures log only a stable stage and exception type,
never exception text or traceback content.

## Development verification

```powershell
uv run pytest -q tests/test_sitefilter_native_host.py
```

## Windows installation

The fixed unpacked-extension identity is derived from the public-only
`manifest.key`: extension ID `jaihdgjnnpmiabeoefmihmjhoodcjlhf`, origin
`chrome-extension://jaihdgjnnpmiabeoefmihmjhoodcjlhf/`. The private key used
to establish that identity is not retained. Task 7 must copy this exact public
key into the extension manifest.

Run the installer from a normal (non-administrator) Windows PowerShell 5.1 or
newer session:

```powershell
.\integrations\sitefilter-native-host\install-native-host.ps1 -ProjectPath C:\path\to\universal_web_collector_v9
```

Omit `-ProjectPath` to select the project with a Windows folder picker. Use
`-Distro <name>` to override default WSL detection, `-SkipProvision` to skip
the WSL `make install` and `make build` steps, and `-WhatIf` to validate and
print the complete plan without changing files or the registry. The installer
pins PyInstaller 6.16.0, builds a complete sibling staging installation, and
performs BOM-free atomic JSON writes before swapping it into
`%LOCALAPPDATA%\SiteFilter\NativeHost`, writes the exact config and manifest,
restricts that directory to the current user, and registers both Chrome and
Edge under HKCU. A failure after the root swap, either registry write, or final
self-check restores the prior root and exact prior registry defaults. If
Windows `uv` is initially absent, the installer downloads the pinned official
uv 0.12.15 archive, verifies SHA-256
`477BD99A84E34891F2BD4C9152DDEB74E971ACCCCBC59C0F0301F11F08A32D46`
before extraction, resolves
the absolute per-user executable, and refreshes its own `PATH`. It never needs
elevation.
If a target without this installer's ownership marker contains a colliding
host filename, installation refuses without changing it. Non-colliding
unexpected files are preserved through a transactional replacement.

Uninstall only the owned host files and matching registrations:

```powershell
.\integrations\sitefilter-native-host\uninstall-native-host.ps1
```

The versioned ownership marker enumerates every generated file and stores only
the prior typed default value for each exact hardcoded browser host key.
Named values and subkeys are never serialized or replayed. Uninstall restores
or removes a prior default only while the current default still points to this
installation, preserves later changes, removes only recorded files, and
removes `NativeHost` only when it is empty. Before mutation it validates any
owned runtime PID against the configured WSL distribution, project cwd, and
backend command, requests bounded graceful termination, and checks every owned
file for locks.
Unexpected files added after installation remain in place. The uninstaller
does not remove `%LOCALAPPDATA%\SiteFilter`, Collector data, downloads,
browser profiles, or project environments.

The hidden `-InstallRoot`, `-RegistryRoot`, `-TestHostExecutable`,
failure-injection, uv-fixture, process-verification, root-safety, and
`-SkipSelfCheck` parameters are exclusively for disposable integration tests:

```powershell
.\integrations\sitefilter-native-host\test-install-native-host.ps1 -ProjectPath (Get-Location)
```

For release verification, `test-browser-native-host.ps1` creates a temporary
unpacked probe extension and browser profile, sends one native `ping`, then
removes both. `-Browser Chromium` automatically uses Playwright's supported
Chromium build, while `-Browser Edge -BrowserPath <path>` tests Edge. The
optional `-Browser ChromeForTesting` mode downloads the official stable win64
artifact into an explicit LocalAppData cache and prints its version and archive
SHA-256; `-ChromeForTestingCacheRoot` changes that cache. None of these modes
opens or changes a normal browser profile.

Installed branded Chrome 153 rejects command-line-loaded unpacked extensions
with `ERR_BLOCKED_BY_CLIENT`, so it is not an automation route. Use Chromium
or Chrome for Testing for the temporary probe; Task 10 can perform acceptance
with a user-loaded extension in branded Chrome.
