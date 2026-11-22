# blastradius

Test selector for the RWX Blast Radius track: given patches against sqlglot
`eb11aec`, print which tests to run for each one.

## How it works

**Offline map** (`tools/`, committed as `map.json.gz`, built from the pinned
commit only):

- per-test line coverage of `sqlglot/` and `tests/`, with import-time code
  kept separate (all modules are imported before measuring);
- class setup (`setUpClass`) coverage, credited to every test in the class;
- which files each test opens (fixtures);
- which sqlglot classes each test instantiates;
- coverage of the code that runs while each test module is imported;
- per-item coverage for tests made of many independent cases (optimizer
  fixture files, TPC-H/TPC-DS queries, per-dialect subtests);
- a static import graph and the names each function reads;
- the CPU each test costs under our runner.

**At run time** (`selector.py`), for each patch:

1. Apply it to an in-memory copy of the changed files.
2. Comment-only edits are proven harmless by comparing ASTs.
3. Changed lines are mapped to the innermost statement. Lines inside function
   bodies select the tests that executed them. Default-argument changes select
   the tests that ran that function. Class flags select the tests that use the
   class and read the flag. Other import-time edits fall back to every user of
   the class or module.
4. The candidate tests are run against the patched code in a forked worker
   from a parent that has already imported sqlglot and the tests:
   - function-body edits swap the new code objects into the live functions;
   - literal defaults and class flags are patched in place;
   - anything else re-imports the changed modules and every loaded module
     holding a reference to them. A module that no longer imports marks every
     test file importing it as failing.
5. Only the tests that actually fail are reported. Tests too slow to run are
   reported only when something cheaper already failed. Once a failure is
   found, the rest are reported rather than run whenever that is cheaper.
6. The reported tests are spread over 1–8 workers to minimise
   `max load + 2 s × workers`.

## Layout

- `selector.py` – the program the image runs
- `map.json.gz` – precomputed map (see above)
- `tools/build_map.py`, `tools/finalize_map.py`, `tools/add_cpu.py`,
  `tools/measure_cpu.py` – how the map is built
- `tools/mutate.py`, `tools/label.py`, `tools/plan_eval.py`,
  `tools/policy_sim.py` – synthetic patches and local evaluation
- `kit/` – the organisers' starter kit and sample set

## Rebuilding the map

From a sqlglot checkout at `eb11aec` with `PYTHONPATH=.` and
`SKIP_INTEGRATION=0`:

```sh
for i in $(seq 0 11); do python tools/build_map.py tests.json out/shard$i.json $i 12 & done; wait
python tools/finalize_map.py tests.json out map.json.gz
python tools/add_cpu.py map.json.gz cpu_measure.json   # from tools/measure_cpu.py in the image
```
