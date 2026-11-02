---
title: GenBot
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Agentic RAG incident copilot over live metrics and logs
---

# GenBot: agentic incident copilot

A live demo of an AI on-call assistant. A small online-shop API runs inside this Space with real traffic,
Prometheus metrics and logs. Break it on purpose in the **Chaos lab**, wait about two minutes, then ask
GenBot what's wrong. It investigates with tools (live metrics, semantic log search, deploy history and a
memory of past incidents) and shows every step.

- Faults clear automatically after 10 minutes, and each visitor can ask 5 questions per session (free LLM quota).
- Alerts also trigger investigations on their own; see the **Alert feed** tab.

Built with FastAPI, Prometheus, Alertmanager, Chroma, SQLite, Streamlit and Groq's free tier.
