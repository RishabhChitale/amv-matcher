# How scoring works

Source: `amv_matcher/score.py` (with `curves.py` and `video.py` for the inputs). Every sub-score is 0-100.

    base  = 0.20 * tempo + 0.60 * sync + 0.20 * arc
    total = max(base, 0.80 * base + 0.20 * build)    # build bonus (U8), only when a song build-up is in the window

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

## Build bonus (U8, `buildup.py`)
A song build-up is rising energy and onset density over 2-16 s that ends at a drop. If one falls inside the trim window, `build = 0.7 * launch + 0.3 * intensity`, where launch = 0.8 * recall + 0.2 * precision of video cuts/peaks landing on the drops (within +-1 beat, clipped to 0.25-0.8 s). The blend can never lower the total. With no build-up in the window the total is just `base`. The response also carries `build_pct`, `launch_pct`, `n_launches`, `n_builds`.

Decisions use the pre-bonus total: splice accepts or rejects a layout on `base` (the bonus is not comparable across layouts), and auto-sync ranks runs on `base`, then tightness, then median error.

## Booms and camera motion (F6 / F9, `boom.py`, `flow.py`)
- **Camera motion**: one optical-flow pass splits camera motion (pan/tilt/zoom/whip from a similarity fit) from subject motion. Camera speed feeds the video energy curve for arc with weight `CAM_ENERGY_W` 0.35.
- **Camera events are off as sync events** (`CAM_EVENT_WEIGHT` and `CAM_WHIP_WEIGHT` are 0): adding them lowered the scores of real own pairs in the ground-truth check, so they are left out of sync.
- **Booms**: song booms (`drop`, `initial_boom`, `encore`, `boom`) and visual booms (subject impacts, flashes, whips) give `boom_pct`, how well the song's main boom lands on a visual boom. It is reported separately and is **not** part of `total` (`BOOM_IN_TOTAL = False`) until recall on the user-reviewed booms is good. Measured 2026-11-11 against the user's review (strict +-50 ms audio, +-80 ms video): audio recall 0.33, video recall 0.71, so it stays out. With or without the bonus the ground-truth table is unchanged (own pairs 85-97, cross pairs at most 48).
- Auto-sync still has a `boom` step that may move the offset to land the main boom, but only when the total does not drop.

## Tempo sweep (`tempo.py`)
For the top 5 ranked songs only (lazy, `TEMPO_SWEEP_TOP`), the server tries song speeds 0.90x-1.10x: coarse steps of 0.02, then 0.01 around the best. Each ratio rescales the cached analysis (no re-analysis), and the offset is carried over and nudged by up to +-0.1 s. The result is `best_tempo_ratio`, `best_tempo_total_pct` and a label such as "fits best at +4% speed". The original soundtrack is skipped.

## Auto-sync (`autosync.py`)
- **Energy first** (`prefer_energy`, default on): arrange ranks by energy correlation and keeps the highest-arc arrangement that stays within the baseline's total, tightness and sync guards.
- **Deterministic budget**: work is measured in units (tempo 5, splice 4, arrange 6, retime 3, retime_alt 1, micro 1; 100 by default). The unit budget alone decides the result, so the same inputs give the same plan on any machine. `final.work_units` reports {used, budget}.
- **Wall-clock cap**: an emergency abort only, at max(180 s, 10 x budget_s). If it fires, the result carries `deterministic: false` and the limit "budget: not reproducible (wall-clock safety cap hit)". Otherwise `deterministic` is true.
- **Smooth mode** (`smooth: true`, U11 option B): forces a retime-only plain-trim plan with eased speed ramps (no instant speed steps) and applies it even if it scores lower. `plan.retime` is the ramp plan, and `result.smooth` = {applied, reason, smooth: {total, sync, tight, arc, max_jump, max_accel}, best: {same}}, where `best` is what plain auto-sync would have chosen. Looped, original-locked or no-retime cases return the plain result with `applied: false` and a reason. It costs about twice the time of a plain run.

## Original audio and the reference
If the video's own soundtrack is among the songs, it is scored at offset 0 only (it is aligned by construction). The server then shows each other song's difference from it (`vs_original`, total and sync).

## How we checked it
On real media, the videos' own songs scored 81 and 97, while the same videos with each other's songs scored 28 and 43. A third video with its own song scored 84. The scorer is meant to separate a real match from a swap, so that gap is the check.

## Known limits
- **Cross-pair separation**: the gap above comes from a few pairs. It has not been measured on many pairs, so a swap with a steady beat may score close to an own pair. Auto-sync retimes the video toward the song, so a high auto-synced total is not proof that the song suits the video.
- **Build detector**: it is energy and onset density only. A build-up carried by harmony or vocals rather than loudness or onsets can be missed (no bonus). The bonus can only add, so a miss costs at most the bonus.
