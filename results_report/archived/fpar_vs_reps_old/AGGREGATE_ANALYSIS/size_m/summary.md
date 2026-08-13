# size=m - FPAR vs reps_dfp summary

Folders contributing to this bucket, and which reps_dfp variant was used as "the" reps_dfp for each:
  - `perm1100M_1seed_4fs` -> `reps_dfp_hopnorm_part`
  - `perm512M_5seeds_1fs` -> `reps_dfp`
  - `tornado1024M_5seeds_5fs` -> `reps_dfp`
  - `tornado1100M_1seed_3fs` -> `reps_dfp`
  - `tornado3600M_5seed_4fs` -> `reps_dfp_hopnorm_part`
  - `tornadoM1100_seed3_4fs` -> `reps_dfp_hopnorm_part`

Mean FCT, FPAR vs reps_dfp, overall: **+63.6%** (n=32 cells)
  - load<=0.5: +81.4% (n=9)
  - load>=0.75: +56.7% (n=14)

(positive = FPAR worse/slower than reps_dfp; negative = FPAR better)
