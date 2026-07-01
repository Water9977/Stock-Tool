## gstack (recommended)

This project uses [gstack](https://github.com/garrytan/gstack) for AI-assisted workflows.
Install it for the best experience:

```bash
git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
cd ~/.claude/skills/gstack && ./setup --team
```

Skills like /qa, /ship, /review, /investigate, and /browse become available after install.
These are optional — use them when specified or when they clearly fit the task at hand,
not as a default replacement for existing tools (e.g. mcp__claude-in-chrome__* remains the
default for web browsing; /browse is an alternative available on request).
Use ~/.claude/skills/gstack/... for gstack file paths.
