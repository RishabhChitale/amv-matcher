# Third-party notices

AMV Matcher bundles or optionally downloads the third-party software below. Versions and license names come from the installed package metadata (`importlib.metadata`) at the time of writing; the license text of each package ships in its own distribution. This file is an inventory, not legal advice.

## Distribution notes (copyleft items that affect shipping)

1. **FFmpeg inside PyAV (needs attention).** The `av` 15.1.0 wheel bundles shared FFmpeg libraries (libavcodec 61.19.101, libavformat 61.7.100, libavutil 59.39.100, libswscale, libswresample, libavfilter, libavdevice) in `av/.dylibs`. Their build configuration (read from the binaries) has `--enable-version3` and `--enable-shared` but not `--enable-gpl`, and the libraries report "LGPL version 3 or later". **However the same build also links `libx264` (GPL-2.0-or-later) and `libx265` (GPL-2.0-or-later or commercial), plus libopencore-amr (Apache-2.0), libmp3lame (LGPL), libtwolame (LGPL), libspeex, libvpx, libaom, SVT-AV1, dav1d, libopenh264, libsrt (MPL-2.0), libgmp (LGPL/GPL).** `av.codecs_available` includes `libx264` and `libx265`. Shipping an app that bundles these means the GPL (x264/x265) obligations may apply to the whole distribution, not only the LGPL ones. Options: (a) comply with GPL (offer corresponding source, GPL license text, GPL-compatible overall licensing), (b) use a PyAV/FFmpeg build without libx264/libx265 and use `h264_videotoolbox` / libopenh264 instead, or (c) obtain a commercial x264/x265 license. The LGPL parts additionally require that FFmpeg stays dynamically linked (it is: separate `.dylib` files) and that users can replace them. Source for FFmpeg: https://ffmpeg.org/ (https://ffmpeg.org/legal.html); PyAV: https://github.com/PyAV-Org/PyAV. License text source: `av-15.1.0.dist-info/licenses/LICENSE.txt` (PyAV, BSD-3-Clause); FFmpeg's own license text is not included in the wheel and should be added from ffmpeg.org when shipping.
2. **PyInstaller 6.22.3** (build tool): GPL-2.0-or-later *with a special exception* that allows bundling the bootloader into apps under any license. No copyleft applies to the built app's own code.
3. **Optional downloads** (RIFE, Demucs/torch) are fetched by the user, not shipped in the installer; keep their notices with the downloaded files.
4. Windows-only optional toast libraries (`winotify` / `win10toast`) are not installed on the macOS dev venv; I could not read their metadata, so verify their licenses before bundling them on Windows.

## Components

| Component | Version | License | Copyright / source |
|---|---|---|---|
| PyAV (`av`) | 15.1.0 | BSD-3-Clause | Copyright retained by original committers. https://github.com/PyAV-Org/PyAV |
| FFmpeg libraries (bundled in PyAV) | libavcodec 61.19.101 | LGPL-3.0-or-later (this build); see note 1 | Copyright the FFmpeg developers. https://ffmpeg.org |
| x264 / x265 (bundled in PyAV) | libx264.165 / libx265.215 | GPL-2.0-or-later | x264: VideoLAN; x265: MulticoreWare. https://www.videolan.org/developers/x264.html, https://www.x265.org |
| OpenCV (`opencv-python-headless`) | 5.0.0.93 | Apache-2.0 (wrapper and OpenCV 4.5+) | https://github.com/opencv/opencv-python |
| librosa | 0.11.0 | ISC | Copyright librosa development team. https://librosa.org |
| numpy | 2.0.2 | BSD-3-Clause | Copyright (c) 2005-2024, NumPy Developers. https://numpy.org |
| scipy | 1.13.1 | BSD-3-Clause | Copyright (c) 2001-2002 Enthought, Inc.; 2003 SciPy Developers. https://scipy.org |
| numba (librosa dependency) | 0.60.0 | BSD-2-Clause | https://numba.pydata.org |
| soundfile | 0.13.1 | BSD-3-Clause | https://github.com/bastibe/python-soundfile (libsndfile is LGPL-2.1, bundled in its wheel on some platforms) |
| FastAPI | 0.128.8 | MIT | https://github.com/fastapi/fastapi |
| Starlette | 0.49.3 | BSD-3-Clause | https://github.com/encode/starlette |
| uvicorn | 0.39.0 | BSD-3-Clause | https://github.com/encode/uvicorn |
| pydantic | 2.13.5 | MIT | https://github.com/pydantic/pydantic |
| pywebview | 6.2.1 | BSD-3-Clause | https://github.com/r0x0r/pywebview (pulls in `bottle` 0.13.4 MIT and `proxy_tools` 0.1.0 MIT) |
| platformdirs | 4.4.0 | MIT | https://github.com/tox-dev/platformdirs |
| Pillow | 11.3.0 | MIT-CMU (HPND) | https://python-pillow.org (used by tools/icon generation) |
| psutil (optional, battery check) | not installed in the dev venv | BSD-3-Clause | https://github.com/giampaolo/psutil |
| PyInstaller | 6.22.3 | GPL-2.0-or-later with bootloader exception | https://pyinstaller.org |
| RIFE ncnn-vulkan (optional download) | 20221029 | MIT | Copyright (c) 2020 nihui. https://github.com/nihui/rife-ncnn-vulkan. License text read from `.cache/tools/rife/rife-ncnn-vulkan-20221029-macos/LICENSE` |
| RIFE model `rife-v4.6` (optional download) | v4.6 | MIT (model weights from the Practical-RIFE project) | https://github.com/hzwer/Practical-RIFE. The model folder ships without its own license file: confirm upstream before redistributing |
| Demucs (optional, `.venv-stems`) | 4.0.1 | MIT | Copyright Meta Platforms, Inc. https://github.com/facebookresearch/demucs. Pretrained model weights are separate downloads with their own terms |
| torch (optional) | 2.2.2 | BSD-3-Clause | Copyright (c) 2016- Facebook, Inc. (Adam Paszke) and others; see the torch `LICENSE` and `NOTICE`. https://pytorch.org |
| torchaudio (optional) | 2.2.2 | BSD-2-Clause | https://github.com/pytorch/audio |

watchdog is not used by the app.

## License texts

Notices for the MIT, BSD and ISC packages above are satisfied by keeping each package's own `LICENSE` file (shipped in its dist-info) with the app. The standard forms are:

**MIT** — Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

**BSD-3-Clause** — Redistribution and use in source and binary forms, with or without modification, are permitted provided that: (1) redistributions of source code retain the copyright notice, this list of conditions and the disclaimer; (2) redistributions in binary form reproduce them in the documentation and/or other materials provided with the distribution; (3) the name of the copyright holder and its contributors may not be used to endorse or promote derived products without specific prior written permission. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED; IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE.

**Apache-2.0 / LGPL / GPL** — full texts: https://www.apache.org/licenses/LICENSE-2.0, https://www.gnu.org/licenses/lgpl-3.0.html, https://www.gnu.org/licenses/gpl-2.0.html. Include them in the shipped app when distributing OpenCV, FFmpeg and x264/x265.
