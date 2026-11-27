# Tech Digest Agent — 科技新訊摘要

A personal agent that lives on a [Fly.io Sprite](https://fly.io/sprites/) and
publishes a daily Traditional Chinese (zh-TW) tech-news digest.

Built for **Fly.io Hacksgiving 2026**.

## What it does

1. **Fetches** the latest items from 18 AI-focused RSS/Atom feeds, with a
   round-robin interleave so every source is represented (up to 60 items):
   official frontier-lab and big-tech research blogs (OpenAI, Google
   DeepMind, Google AI, Google Research, Mistral AI, Microsoft Research,
   Apple ML Research, NVIDIA AI, AWS Machine Learning, Hugging Face),
   arXiv preprints (cs.AI, cs.CL), and curated coverage (Hacker News,
   Ars Technica, TechCrunch AI, The Verge AI, Simon Willison, Import AI).
   Note: Anthropic, Meta AI and xAI publish no RSS feed, so they cannot be
   collected this way.
2. **Summarizes** the collection into a zh-TW digest of the top 9 stories
   with an LLM reached through the **Sprites Ollama connector** — the
   Sprite never holds the Ollama API key; the Sprites gateway attaches the
   organization's credential to the request. All displayed text (feed items
   and LLM output alike) is force-converted to Taiwan Traditional Chinese
   with [@willh/opencc-cli](https://www.npmjs.com/package/@willh/opencc-cli)
   (cn → tw2).
3. **Publishes** the digest as a responsive web page (card grid, source
   badges, dark-mode support) served from the same Sprite, plus an RSS
   feed of *all* collected items at
   https://tech-digest-b6vs6.sprites.app/feed.xml — and regenerates
   everything every 6 hours.

Live demo: https://tech-digest-b6vs6.sprites.app

## Layout

- `agent.py` — feed collection, LLM summarization (via the connector
  gateway), and HTML rendering. Falls back to a titles-only digest when no
  connector is configured.
- `serve.py` — tiny static server (the Sprite routes its public URL to
  port 8080).
- `run.sh` — service entry point: generates the digest, starts the server,
  then regenerates every 6 hours.

## Running on a Sprite

```sh
# inside the Sprite
sprite-env services create digest --cmd /bin/bash --args /home/sprite/tech-digest/run.sh
```

Set `GATEWAY_BASE` to the connector gateway base URL
(`https://api.sprites.dev/v1/gateway/ollama/<connection_id>`) to enable LLM
summaries; optionally set `OLLAMA_MODEL` to pin a model.
