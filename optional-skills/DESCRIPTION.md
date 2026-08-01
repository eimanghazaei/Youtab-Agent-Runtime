# Optional Skills

Official skills maintained by Youtab B.V. that are **not activated by default**.

These skills ship with the youtab-agent-runtime repository but are not copied to
`~/.youtab-agent-runtime/skills/` during setup. They are discoverable via the Skills Hub:

```bash
youtab skills browse               # browse all skills, official shown first
youtab skills browse --source official  # browse only official optional skills
youtab skills search <query>       # finds optional skills labeled "official"
youtab skills install <identifier> # copies to ~/.youtab-agent-runtime/skills/ and activates
```

## Why optional?

Some skills are useful but not broadly needed by every user:

- **Niche integrations** — specific paid services, specialized tools
- **Experimental features** — promising but not yet proven
- **Heavyweight dependencies** — require significant setup (API keys, installs)

By keeping them optional, we keep the default skill set lean while still
providing curated, tested, official skills for users who want them.
