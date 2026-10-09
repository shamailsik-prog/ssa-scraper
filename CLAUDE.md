## Skills: Superpowers first, gstack on request

**Superpowers is the default workflow.** Use its skills for all work in this repo:
brainstorming and writing-plans before building, test-driven-development while
building, systematic-debugging for any failure, and verification-before-completion
before claiming anything is done, fixed or passing.

**gstack is on-demand only.** Use gstack skills (/qa, /review, /investigate, /cso,
/ship, /browse and the rest) only when the user explicitly asks for gstack, for
example "check it through gstack" or "review it with gstack". Do not reach for
gstack otherwise, including for web browsing.

If gstack is requested but missing, install it:

```bash
git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
cd ~/.claude/skills/gstack && ./setup --team
```

## Behaviour skill (owner's working standards)

Load the `behaviour` skill (`.claude/skills/behaviour/SKILL.md`) before any substantive
task. It sets the owner's standards for analysis depth, adversarial testing, verification
of legal authority (never invent citations; mark doubtful ones **[REQUIRES VERIFICATION]**),
court-ready drafting format, and the mid-chat trigger words (`GODMODE`, `OODA`, `STEELMAN`,
`DEVIL`, `CRITIQUE`, and the rest).
