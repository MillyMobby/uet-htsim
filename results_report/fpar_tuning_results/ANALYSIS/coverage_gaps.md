# Coverage gap report

Intended grid per pattern is approximated as the UNION of loads / flowsizes / seeds / strategies actually observed anywhere for that pattern in this folder's summary.csv (there is no external spec to compare against, so this only catches *relative* gaps - one strategy tested less than its siblings in the same sweep).

## pattern=tornado
- fewer seeds: strat=`fpar_tq1` load=0.25 flowsize=500000 -> 4/5 seeds (have ['2', '3', '4', '5'])
- fewer seeds: strat=`fpar_tq1` load=1.0 flowsize=10000000 -> 3/5 seeds (have ['1', '2', '3'])
- fewer seeds: strat=`fpar_tq12` load=1.0 flowsize=10000000 -> 1/5 seeds (have ['2'])
- **MISSING ENTIRELY**: strat=`fpar_tq24` load=1.0 flowsize=10000000 (other strategies have data here)
- fewer seeds: strat=`fpar_tq3` load=1.0 flowsize=10000000 -> 3/5 seeds (have ['1', '2', '3'])
- fewer seeds: strat=`fpar_tq6` load=1.0 flowsize=10000000 -> 3/5 seeds (have ['1', '2', '3'])
- fewer seeds: strat=`reps_dfp_best` load=1.0 flowsize=10000000 -> 2/5 seeds (have ['1', '2'])
