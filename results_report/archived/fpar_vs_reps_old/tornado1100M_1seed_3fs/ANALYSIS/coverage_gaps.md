# Coverage gap report

Intended grid per pattern is approximated as the UNION of loads / flowsizes / seeds / strategies actually observed anywhere for that pattern in this folder's summary.csv (there is no external spec to compare against, so this only catches *relative* gaps - one strategy tested less than its siblings in the same sweep).

## pattern=tornado
- **MISSING ENTIRELY**: strat=`fpar` load=0.5 flowsize=10000000 (other strategies have data here)
- **MISSING ENTIRELY**: strat=`fpar` load=0.75 flowsize=2000000 (other strategies have data here)
- **MISSING ENTIRELY**: strat=`fpar` load=0.75 flowsize=10000000 (other strategies have data here)
- **MISSING ENTIRELY**: strat=`fpar` load=1.0 flowsize=2000000 (other strategies have data here)
- **MISSING ENTIRELY**: strat=`fpar` load=1.0 flowsize=10000000 (other strategies have data here)

## Attempted-but-not-in-summary.csv runs (crashed / timed out)

**5 run(s) hit the sweep's timeout cap** (excluded from summary.csv):
  - 5 run(s) at the 800s cap
