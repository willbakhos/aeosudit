# AEO Audit

Audit how a website appears across AI answer engines (Claude, ChatGPT, Perplexity, Gemini, Google AI Overviews). Produces a CSV + single-file HTML report covering visibility, citations, competitor share-of-voice, and hallucinations.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in OPENROUTER_API_KEY and APIFY_TOKEN
```

## Run

```bash
python -m src.main audit --config config/site.yaml
```

Outputs land in `output/{timestamp}/`:
- `results.csv` — one row per (query × engine), with all scoring columns
- `report.html` — visualisation: headline metrics, engine heatmap, top cited sources, competitor share-of-voice, hallucination flags, per-query drill-down
- `raw_responses.json` — archived raw engine responses (rerun scoring offline without re-paying for API calls)
- `errors.log` — any per-call errors (other engines still complete)

## Flags

| Flag | Default | Notes |
|---|---|---|
| `--config` | required | Path to `site.yaml` |
| `--queries` | `config/queries.csv` | Two columns: `query,type` |
| `--output-dir` | `output` | Root; a timestamped subfolder is created per run |
| `--skip-llm-scoring` | off | Skip the Haiku sentiment/accuracy pass (faster + cheaper) |
| `--engines` | all | Comma-separated labels, e.g. `Claude,Perplexity` |

## Reusing for a different site

Swap the config — no code changes:

```bash
python -m src.main audit --config config/ryvet.yaml
```

A site config defines: brand name + aliases + domain, competitor list, ground-truth facts (used by the LLM scorer), locale, and which engines to query.

## Architecture notes

- All engine I/O is async with a max concurrency of 10. 40 queries × 5 engines typically completes in < 5 minutes.
- OpenRouter calls use the **native** web-search tool — what each provider's product actually shows users.
- Apify runs `apify/google-search-scraper` for Google AI Overviews / AI Mode.
- Errors in one engine don't block the others; failures are written to `errors.log` and excluded from rate metrics.
- Scoring is two-pass: deterministic (regex on text + URL parse on citations) → LLM (Haiku via OpenRouter for sentiment, accuracy vs ground truth, and hallucination detection).
- Model IDs in `site.yaml` should be verified against current IDs on openrouter.ai/models before each run.
- Results drift run-to-run even with identical queries — that's expected; don't chase determinism.

## Project layout

```
config/        site.yaml + queries.csv
src/           Python package
  engines/     OpenRouter + Apify adapters (extend by adding to engines/)
  models.py    pydantic schemas
  runner.py    async fan-out
  scorer.py    deterministic scoring
  llm_scorer.py Haiku scoring pass
  report.py    CSV + HTML generation
  main.py      CLI
templates/     report.html.j2
output/        gitignored; timestamped run subfolders
```
