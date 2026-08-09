# Coverage gap report

Intended grid per pattern is approximated as the UNION of loads / flowsizes / seeds / strategies actually observed anywhere for that pattern in this folder's summary.csv (there is no external spec to compare against, so this only catches *relative* gaps - one strategy tested less than its siblings in the same sweep).

## pattern=permutation
- fewer seeds: strat=`fpar` load=0.6 flowsize=10000000 -> 2/3 seeds (have ['2', '3'])
- fewer seeds: strat=`fpar` load=0.7 flowsize=2000000 -> 2/3 seeds (have ['1', '2'])
- **MISSING ENTIRELY**: strat=`fpar` load=0.7 flowsize=10000000 (other strategies have data here)
- **MISSING ENTIRELY**: strat=`fpar` load=0.8 flowsize=2000000 (other strategies have data here)
- **MISSING ENTIRELY**: strat=`fpar` load=0.8 flowsize=10000000 (other strategies have data here)
- fewer seeds: strat=`fpar` load=0.9 flowsize=2000000 -> 1/3 seeds (have ['1'])
- **MISSING ENTIRELY**: strat=`fpar` load=0.9 flowsize=10000000 (other strategies have data here)

## Attempted-but-not-in-summary.csv runs (crashed / timed out)

**16 run(s) hit the sweep's timeout cap** (excluded from summary.csv):
  - 16 run(s) at the 800s cap
