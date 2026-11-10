# Builds (private)

Builds are for personal use only: the bundled FFmpeg includes GPL components (libx264/libx265); do not redistribute. See `THIRD-PARTY-NOTICES.md`. There are no GitHub Releases and no public binaries.

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
