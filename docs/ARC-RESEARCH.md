# ARC research (G-ARC, leftover L4)

Branch `g-arc` (off origin/main 7b15d70). NOT for merge. Code: `amv_matcher/arc_research.py`
(research copy of arrange.py; knobs `ARC_GUARD_TOTAL`, `ARC_GUARD_TIGHT`, `ARC_GUARD_SYNC`,
`ARC_MODE` = `arc`|`base`, `PROXY_MODE`). `extra_targets` stays None by default.

Question: can retiming calm video blocks onto song lulls, plus an arc-aware arrange step, push arc >= 85
on "A + S1" without hurting sync / tight / total?

## Bounds (numeric oracles)
- arc = 50*(r+1) + bonus (bonus capped at 20); r = video-vs-song energy Pearson. A+S1 has r = 0.09 -> arc ~74. arc >= 85 needs r >= ~0.31.
- The song has one lull, the video about nine, so lull alignment by retime alone is mostly impossible.
- Retime-only oracle: r ~ 0.30 at best (arc ~85 upper bound, not reachable in practice).
- Section-reorder oracle: r 0.30 / 0.40 / 0.49 with 2 / 3 / 4 segments -> >= 85 is energy-feasible only by rearranging.

## Measured (real media, numeric only, budget 20 s)
| pair | variant | total | sync | arc | tight | effort | time | path |
|---|---|---|---|---|---|---|---|---|
| A + S1 | base | 93 | 98.0 | 71.8 | 83.2 | 4.8 | ~15 s | retime_only |
| A + S1 | arc, 3 moves | 94 | 95.8 | 83.3 | 65.3 | 36.1 | 15.1 s | default |
| A + S1 | arc, 6 moves | = base (fell back) | | | | | | retime_only |
| B + S1 | base | 95 | 97.7 | 81.0 | 90.6 | 23.6 | 9.7 s | retime_first |
| B + S1 | arc, 3 moves | 95 | 97.7 | 81.0 | 90.6 | 23.6 | 7.5 s | retime_first (no change) |
| A + own | base | 93 | 93.0 | 88.2 | 30.5 | 12.1 | 6.0 s | splice_first |
| A + own | arc, 3 moves | 92 | 92.1 | 89.4 | 26.3 | 20.8 | 6.4 s | splice_first |

Own/cross pairs `@own:B` (A) and `@own:A` (B) could not be measured: the harness raised KeyError resolving the cross-case token (harness issue, not investigated further). Synthetic fixtures were not re-run.

## Conclusions
- arc >= 85 is not reachable by retime alone. Only the arc-aware multi-move arrange gets close (71.8 -> 83.3), and it costs tight_pct 83.2 -> 65.3 and sync 98.0 -> 95.8 (total +1).
- B + S1 already has arc 81 and is unchanged; the own pair A got arc +1.2 but tight -4.2, sync -0.9, total -1: worse.
- Runtime stays within 20 s.
- Recommendation: do not merge as default. Keep, if at all, behind an off-by-default flag; otherwise drop. The tight/sync trade makes it a net loss on the user's own pairs.
