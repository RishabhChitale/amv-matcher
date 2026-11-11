# Builds (private)

Builds are for personal use only: the bundled FFmpeg includes GPL components (libx264/libx265); do not redistribute. See `THIRD-PARTY-NOTICES.md`. The only GitHub Releases are encrypted (below); there are no unencrypted binaries anywhere.

## How CI delivers a build
Each push, pull request or manual run builds and self-tests the app. If the repository secret `AMV_ARTIFACT_KEY` is set, the app is packed into an AES-256 encrypted 7z (header encryption on) and uploaded as the Actions artifact `AMV-Matcher-<label>-encrypted`, kept for **7 days**. If the secret is missing, no binary is uploaded at all; only `selftest.log` is, and the run prints "AMV_ARTIFACT_KEY not set: no binary artifact uploaded".

## Set the secret (you do this)
GitHub repo > Settings > Secrets and variables > Actions > New repository secret, name `AMV_ARTIFACT_KEY`, value = your password. Keep your own record of it; it is never stored in the repo.

## Download and decrypt
1. Open the run on the Actions tab, scroll to Artifacts, download `AMV-Matcher-<label>-encrypted` (a zip containing the .7z).
2. Unzip it, then `7z x AMV-Matcher-<label>.7z` and enter the password.
3. **macOS:** the 7z contains an inner zip made with `ditto` so the app bundle (symlinks, permissions, extended attributes) survives. Extract it with `ditto -x -k AMV-Matcher-<label>.zip <dest>` (Archive Utility also works). Do not re-zip the app with other tools.
4. **Windows:** extracting the 7z gives the app folder directly.
5. Move the app where you like (first-launch warnings are in the README).

## Password handling (honest note)
The password is passed to `7z` on the runner's command line. GitHub masks it in logs, and the runner is ephemeral, but it is briefly visible in that runner's process list. The intermediate macOS zip is created outside `enc/` and deleted in the same step, so it is never uploaded.

## GitHub Releases (encrypted)
Pushing a tag `v<major>.<minor>.<patch>[-suffix]` (or running the workflow manually with the `tag` input set to an existing tag) builds the selected targets (Windows only by default), and only when every selected leg is green (tests, build, selftests, encrypted packaging) a `release` job publishes a GitHub Release. Versions below 1.0 are marked pre-release.

- **Assets:** by default exactly two: `AMV-Matcher-<ver>-windows.7z` and `SHA256SUMS.txt`. `-macos-arm.7z` and `-macos-intel.7z` are added only when the workflow was run manually with `platforms` = `macos` or `all` plus a `tag`. They are the same AES-256, header-encrypted archives as the Actions artifacts, renamed. Nothing else is attached.
- **Guards (the job fails otherwise):** each asset has the 7z signature, listing it without a password fails (headers encrypted), and listing it with the key shows `Encrypted = +` for every entry and no `Encrypted = -`. A missing `AMV_ARTIFACT_KEY` also fails the job; nothing is released unencrypted.
- **Key handling:** the secret is only read into the environment of the verify step, never echoed, and passed to `7z` the same way as in the build step (see Password handling). The release job is the only one with `contents: write`.
- **Notes:** commit subjects since the previous tag (a final tag compares with the previous final tag, not an rc) plus a fixed install block: 7-Zip, password = the owner's key, SmartScreen "Run anyway", macOS right-click > Open, `ditto -x -k` for the inner zip.
- **To release:** `git tag v0.9.0 && git push origin v0.9.0`. Try `v0.9.0-rc1` first; remove a bad one with `gh release delete v0.9.0-rc1 --cleanup-tag`.
- Docs-only pushes to a branch do not start a build (`paths-ignore`); tag pushes always do.

## Which targets build when
Windows is the primary target. Push, pull-request and tag builds run the Windows job only (full test suite, frozen selftest with the 8-line check and RSS guard, encrypted packaging). The two macOS jobs run nightly at 03:00 UTC (`schedule`, so Mac breakage is still noticed; no release comes from it) and on manual dispatch with the `platforms` input: `windows` (default), `macos` or `all`. A release only waits for, and only publishes, the platforms that were built for it; macOS legs never block a Windows release.
