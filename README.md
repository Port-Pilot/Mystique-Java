# Mystique-Java

`run_mystique_java.py` runs the exported Java dataset through the unchanged
Mystique implementation, sends model requests to the configured local
OpenAI-compatible endpoint, and stores results in
`backport_benchmark_results_mystique_java`.

Install the dependencies in a Python 3.11 environment (the upstream project is
tested with Python 3.11), configure Joern as required by Mystique, and set
`NEON_DATABASE_URL` in `.env` or the environment:

```bash
python3 -m pip install -r requirements-mystique-java.txt
python3 run_mystique_java.py --build-input --case JavaBackports-crate-3
```

The default model is `gpt-5.5` and the default API base URL is
`http://localhost:8317/v1`. Use `--count 1 --dry-run` for a one-case check that
does not write to Neon. Subsequent runs can omit `--build-input` and reuse
`cve-java-custom-full.json` and Mystique's analysis cache.

To rebuild the input with and run the first five dataset cases automatically:

```bash
python3 run_mystique_java.py --build-input --count 5 --dry-run
```

Change `5` to any positive number. `--limit` remains an alias for `--count`.

API cost is read from response usage when the endpoint supplies it. Otherwise,
provide per-million-token rates explicitly:

```bash
python3 run_mystique_java.py \
  --input-cost-per-million INPUT_RATE \
  --output-cost-per-million OUTPUT_RATE \
  --reasoning-cost-per-million REASONING_RATE
```

If neither response cost nor explicit rates are available, `api_cost` is stored
as `NULL` rather than recording an inaccurate value.
