# Telegram Reply Stats

Analyze reply delays in a private Telegram conversation and generate a self-contained
HTML report, CSV summaries and JSON data. Optional recurring sleep schedules and
IANA time zones separate elapsed time from configured awake waiting time.

## Offline demo

Python 3.10+:

```sh
python -m pip install -r requirements.txt
python reply_stats.py --demo --config config.example.json --output output
python -m unittest discover -p "test_*.py" -v
```

Open `output/report.html`. The demo uses generated messages and never connects to Telegram.
`examples/report.html` is a checked-in synthetic example.

## Your own conversation

```sh
python reply_stats.py --chat USERNAME --since 2026-01-01 --config config.example.json
```

Obtain your API ID/hash at https://my.telegram.org. The tool prompts for account
credentials; optional `TG_API_ID`/`TG_API_HASH` environment variables are supported.
Without `--chat`, choose a personal conversation interactively. `--saved` optionally
sends the report ZIP to your own Saved Messages; it is off unless requested.
See [the detailed Russian guide](docs/guide.ru.md) for options and methodology.

## Reports and privacy

The report excludes message text and media contents. Timestamps and interaction
patterns remain sensitive. Telegram authorization is stored locally in `private/`;
do not share that folder. Sleep is a configured schedule, not a measurement of actual
sleep, and these statistics do not infer intentions or explain why somebody replied later.

Русский: анализ задержек ответов в Telegram с HTML-графиками и экспортом CSV/JSON.
