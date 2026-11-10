# Proposal: pin transitive deps in CI (NOT applied)

`.github/workflows/build.yml` is unchanged; this needs the dispatcher's OK.

Problem: `pip install -r requirements.txt` pins only direct deps, so transitive packages can drift between CI builds.

Caveats:
- `constraints-macos.txt` was frozen from a local macOS x86_64 **Python 3.9.6** venv, but CI uses Python **3.11**. Some pins (numba/llvmlite, scipy, numpy) may have no 3.11 wheel at those exact versions, or differ per platform. Verify on a CI run before relying on it.
- Windows and macOS-arm need their own files (platform-specific packages such as pyobjc on macOS, pythonnet/pywin32 on Windows).

## Diff

```diff
       - name: Install
+        shell: bash
         run: |
           python -m pip install --upgrade pip
-          pip install -r requirements.txt -r requirements-dev.txt
+          case "${{ matrix.label }}" in
+            macos-intel) C=constraints-macos.txt ;;
+            *) C=constraints-${{ matrix.label }}.txt ;;
+          esac
+          if [ -f "$C" ]; then
+            pip install -r requirements.txt -r requirements-dev.txt -c "$C"
+          else
+            pip install -r requirements.txt -r requirements-dev.txt
+          fi
+          pip freeze | grep -vE '^-e|@ file' > "frozen-${{ matrix.label }}.txt"
+      - uses: actions/upload-artifact@v4
+        with:
+          name: constraints-${{ matrix.label }}
+          path: frozen-${{ matrix.label }}.txt
```

(Windows runners use Git Bash for `shell: bash`, so the same step works there.)

## First-time generation for macos-arm and windows (documented manual step)

1. Run the workflow once with no constraints file (the `else` branch). A green run uploads `constraints-<label>` artifacts.
2. Download them, rename to `constraints-macos-arm.txt` and `constraints-windows.txt`, add the header comment (date, Python version, how generated), and commit them.
3. From then on CI installs with `-c`. Regenerate the same way when deliberately upgrading dependencies.

Note: the label for Intel macOS is `macos-intel`; if CI-generated files are preferred over the local one, name it `constraints-macos-intel.txt` and drop the special case.
