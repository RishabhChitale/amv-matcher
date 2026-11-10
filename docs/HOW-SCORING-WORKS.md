# How scoring works

Source: `amv_matcher/score.py`. Every sub-score is 0-100; the total is a weighted sum.

    total = 0.20 * tempo + 0.40 * sync + 0.40 * arc

## Length rule
If the song is shorter than the video it is ineligible (score 0, "too short"). Otherwise the video is treated as a window sliding over the song. Start offsets are tried in steps of about 0.1 s (never more than 2000 steps), and the one with the best `0.40*sync + 0.40*arc` wins. That offset gives the trim window (offset to offset + video length).

## Tempo (20%)
Compares the song's BPM with the video's motion tempo. The song BPM is also tried at 0.5x and 2x (`RATES`), and the best match counts, so 70 vs 140 BPM is fine. The score falls off as a bell curve of the relative error (`TEMPO_SIGMA_REL = 0.0679`): 100 at an exact match, 50 at about 8% off, near 0 past about 20%.

## Sync (40%)
Video events are cuts (weight 2.0) and motion peaks (weight 1.0). A peak within 0.1 s of a cut counts once, as a cut. An event is a hit if a song beat is within 70 ms (`TOLERANCE_S = 0.070`) of it, after shifting by the offset. The hit fraction is weighted by event weight.

Beats that are dense would hit by luck, so a chance baseline is subtracted: beats per second in the window times 140 ms, capped at 0.95. Sync = (hit fraction - baseline) / (1 - baseline), clipped to 0-100. Random alignment scores about 0; perfect alignment scores 100.

## Arc (40%)
Compares the shape of the video's energy over time with the song's energy (both sampled at 10 Hz, `ENERGY_HZ`) over the chosen window. The Pearson correlation r (-1 to 1) maps to `50 * (r + 1)`, so no correlation is 50.

Bonuses are then added: +10 for each video peak segment that starts within 0.5 s of a song drop (`DROP_MATCH_S`) and +10 for each video lull that overlaps a song lull (`ARC_BONUS_PER_MATCH`), capped at +20 in total (`ARC_BONUS_MAX`). The result is clipped to 100.

## Boom (not part of the score)
`amv_matcher/pump.py` makes the pulse effect: scale grows by 3% per beat at Boom 100 times the beat strength, drops hit 4x harder (0.15 s match window) and add a brightness flash of up to +35%. Each pulse decays exponentially over the Decay time (default 180 ms).
