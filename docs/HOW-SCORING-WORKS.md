# How scoring works

Source: `amv_matcher/score.py` (with `curves.py` and `video.py` for the inputs). Every sub-score is 0-100.

    total = 0.20 * tempo + 0.60 * sync + 0.20 * arc

## What the video gives the scorer
- **Cuts** (weight 2.0): a frame difference at least 3.5x the local median (within +-0.5 s), at least 1.2x its neighbours, and at least 4 gray levels. Brightness flashes that return within 3 frames and fades through black are not cuts.
- **Motion peaks** (weight 1.0): motion above median + 2.5 x MAD. A peak within 0.1 s of a cut counts once, as a cut.
- **Energy curve** at 10 Hz, with calm stretches (lulls: below 25% energy for 1.5 s or more) and peak segments (top 10% bursts).

## Length rule and offset
A song shorter than the video is ineligible. Otherwise the video is a window sliding over the song. Offsets are scanned in steps of 0.1 s or more (max 2000 steps), the best few are refined in 20 ms steps over +-0.15 s, and cross-correlating the cuts with the song's onsets adds extra candidates. Offsets can be negative, down to -3 s (the song starts after the video, no penalty). The offset gives the trim window.

## Tempo (20%)
The song BPM is compared with the video's tempo and its alternative candidates, also at 0.5x and 2x. The score is a bell curve of relative error (sigma 6.79%): 100 for exact, 50 at about 8% off. It also counts as tempo evidence if the video's events lock to the song's own beat period (see Sync).

## Sync (60%)
Sync asks how sharply the events lock to the song's beat, not just how many land near one. Steps:
1. **Beat grid**: a constant-tempo grid if it fits the music and is within 3% of the song BPM, else the tracked beats.
2. **Lock strength**: the events' beat coherence (Rayleigh: how tightly their phases cluster on one beat period) at the song's period, compared as a z-score with the same measure at wrong periods (+-12%, excluding +-2% around the real one). Few events (under 12) scale the z down.
3. **Logistic**: sync = 100 / (1 + e^(-1.5 * (z - 1.5))), so z = 1.5 gives 50.
4. **Alignment**: the offset must put events about 50 ms before the beat (editors cut slightly early). Within 60 ms of that there is no penalty; beyond it the score falls off as a bell curve.
5. **Fine terms**: 15% of sync is a beat-hit credit (Gaussian, 60 ms) and 5% a raw version of it, so small improvements stay visible once the lock saturates. The credit is chance-corrected for dense beats.

## Arc (20%)
Pearson correlation r of the video and song energy over the overlap maps to `50 * (r + 1)`. Then +10 for each video peak segment starting within 0.5 s of a song drop and each video lull overlapping a song lull, capped at +20. Clipped to 100.

## Original audio and the reference
If the video's own soundtrack is among the songs, it is scored at offset 0 only (it is aligned by construction). The server then shows each other song's difference from it (`vs_original`, total and sync).

## How we checked it
On real media, the videos' own songs scored 81 and 97, while the same videos with each other's songs scored 28 and 43. A third video with its own song scored 84. The scorer is meant to separate a real match from a swap, so that gap is the check.
