# BackportBench Java evaluation

The BackportBench Java pipeline has two stages. First,
`populate_backportbench_java.py` imports eligible Java/Maven relationships and
their Git patches into Neon. Then `run_mystique_backportbench_java.py` reads its
work directly from Neon and stores each generated patch and its usage metrics.
It reuses the wrapper in `../Javabackports/run_mystique_java.py`; the upstream
Mystique checkout is not modified.

The population script reads the workbook, selects rows whose ecosystem is
`maven` and whose backport relationship is `yes`, and uses `which commit is
source` to orient each pair. It obtains both patch texts from the referenced
Git commits and inserts or refreshes rows in
`backport_benchmark_results_mystique_backportbench_java`.

Only the selected commits and their parents are fetched with a partial shallow
fetch. Large projects such as Quarkus are not cloned in full.

Install the same environment used by the custom Java evaluation:

```bash
python3 -m pip install -r ../Javabackports/requirements-mystique-java.txt
```

Configure Joern, `astyle`, the model endpoint, and `NEON_DATABASE_URL` as
described in `../Javabackports/README.md`. Populate Neon first:

```bash
python3 populate_backportbench_java.py
```

New rows receive `status = 'pending'`. Existing rows are refreshed without
changing their status or generated result, so rerunning the importer is safe.

Build the Mystique input and generate every unfinished row:

```bash
python3 run_mystique_backportbench_java.py --build-input
```

After that, resume at any time with:

```bash
python3 run_mystique_backportbench_java.py
```

The generator selects only `pending`, `running`, and `failed` rows. It changes
the current row to `running` before model calls and commits `completed` or
`failed` afterward. Therefore a process killed during a case leaves `running`
in Neon, and the next invocation retries that case while skipping completed
ones. Rows for which Mystique cannot construct a method-level input are marked
`no_usable_patch` and are not retried automatically.

`--case ID` selects one database row, `--count N` limits the number of
unfinished rows, and `--include-completed` explicitly permits regeneration of
completed rows. Use `--dry-run` to avoid status/result updates and `--build-only`
to construct input without model calls.

Database mapping:

- `new_version_patch*`: the workbook's source commit and its Git patch
- `old_version_patch*`: the other commit and its ground-truth Git patch
- `project`: workbook `repository`
- `patch_type`: workbook file/content-change labels
- `file_match` and `content_match`: the corresponding workbook labels
- `programming_language`: `Java`

Compilation and test columns remain `NULL`: Mystique generates a patch but the
workbook does not provide a uniform project build/test command.
