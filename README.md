# Tech Digest Agent — 科技新訊摘要

A personal agent that lives on a [Fly.io Sprite](https://fly.io/sprites/) and
publishes a daily Traditional Chinese (zh-TW) tech-news digest.

Built for **Fly.io Hacksgiving 2026**.

## What it does

1. **Fetches** the latest items from tech RSS/Atom feeds (Hacker News,
   Simon Willison, OpenAI News, Google AI Blog, Ars Technica).
2. **Summarizes** them into a zh-TW daily digest with an LLM reached through
   the **Sprites Ollama connector** — the Sprite never holds the Ollama API
   key; the Sprites gateway attaches the organization's credential to the
   request.
3. **Publishes** the digest as a web page served from the same Sprite, and
   regenerates it every 6 hours.

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
