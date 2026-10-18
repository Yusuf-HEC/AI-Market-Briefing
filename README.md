# AI-Market-Briefing

A daily market briefing generated automatically: it collects news and central bank feeds, asks an LLM to summarize five asset classes with source citations, and emails the result. Built as a learning project in capital markets and applied AI, with AI-assisted coding.

## What it does

1. **Collects** articles from 12 RSS feeds (CNBC, MarketWatch, Yahoo Finance, Investing.com, BBC Business, Les Echos, ECB, Fed press releases and speeches, BIS speeches) over the last 36 hours, plus the FT newsletter if it is received by email.
2. **Routes** each article to one or more asset classes (rates, FX, credit, equities, bonds) with keyword rules.
3. **Analyzes** each asset class with a separate LLM call, using only the articles provided. Every fact must cite its source with a code such as `[T1]` (letter = asset class, number = article order). The model must also explain **why sources disagree** (different data, horizon, editorial angle) and state what the articles do not say.
4. **Synthesizes** the five analyses into one briefing, then saves it in `briefings/` and sends it by email.

## What it is (and is not)

- It is a **fixed LLM workflow** (prompt chaining: 5 analyses, then 1 synthesis). It is **not an autonomous agent**: the code decides every step, the model only writes text.
- The retrieval is plain: RSS + keyword routing. There are no embeddings and no vector database.
- The code was written with AI assistance. The design, the prompts and the evaluation approach are documented below.

## Architecture

| File | Role |
|---|---|
| `briefing.py` | Collection, routing, LLM calls, saving and email |
| `.github/workflows/` | GitHub Actions workflow: schedule, manual run, commit of the briefing |
| `requirements.txt` | Python dependencies |
| `briefings/` | Generated briefings (one Markdown file per day, with sources and token usage in the footer) |
| `evaluer.py` | Evaluation script (see below) |
| `evaluation/` | Saved articles and evaluation results |

## Setup

1. Fork or clone the repository.
2. Add these secrets in **Settings → Secrets and variables → Actions**:
   - `OPENROUTER_API_KEY` : API key used to call the model through OpenRouter
   - `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` : Gmail account and app password (sending, and reading the FT newsletter)
   - Optional: `MAIL_TO` (recipient), `FT_SENDER`, `MODEL` (default `deepseek/deepseek-chat-v3.1`), `PRIX_ENTREE` and `PRIX_SORTIE` (price per million tokens, to estimate cost)
3. Run the workflow manually from the **Actions** tab (*Run workflow*), or wait for the schedule.

**Scheduling.** The workflow runs on weekday mornings through a GitHub Actions cron. GitHub can delay scheduled runs by several hours, so the script only produces one briefing per day, after 7:00 Paris time, and skips the run if the day's briefing already exists.

## Evaluation

`evaluer.py` checks the quality of the model output without running the GitHub workflow and without sending any email. It reuses the collection and generation functions locally.

```bash
pip install openai feedparser trafilatura requests
export OPENROUTER_API_KEY="..."          # PowerShell: $env:OPENROUTER_API_KEY = "..."

python evaluer.py collecter              # save today's articles (freezes the input)
python evaluer.py rejouer --juge         # regenerate a briefing and check it
python evaluer.py rejouer --model <id>   # replay the same articles with another model
python evaluer.py etiqueter              # create a CSV to label by hand
python evaluer.py routage                # routing precision from the labeled CSV
```

What is measured:

- **Invented sources**: citations such as `[T9]` that do not exist among the provided articles (deterministic check).
- **Suspect figures**: numbers in the briefing that do not appear in the cited sources (deterministic check, with false positives on rounded numbers and dates).
- **Faithfulness**: an LLM judge labels each claim `SOUTENUE` / `NON_SOUTENUE` / `NON_VERIFIABLE` against the cited article.
- **Routing precision**: share of articles correctly assigned to an asset class, from a hand-labeled CSV (precision only, no recall).
- **Model comparison**: the same saved articles replayed with different models.

Limits: the judge can be the same model as the author (use `--modele-juge` for a different one), samples are small, and the figure check is a regex, not a semantic check.

## Known limitations

- Summaries depend on what the RSS feeds expose; some feeds block automated requests or return only a short summary.
- The briefing is a reading aid, not investment advice. Figures should be checked against the cited sources.
- Scheduled runs are not punctual (see Scheduling).
