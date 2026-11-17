# Connect Claude

The app runs its own agents. Your Claude Code is a separate thing: it works for you, and it can drive the app too. It does
that through two local connections, which Claude Code calls MCP servers and this app calls doors. This page covers
registering them, and the one sign-in the app's own agents need.

## The two doors

| Door | Name in Claude Code | Address | What it is for |
|---|---|---|---|
| owner | `resumebot-owner` | `http://127.0.0.1:8430/mcp-owner/` | your own Claude runs the app's actions: reading its state, importing your resume, saving your targets, starting a search. Four tools: `actions.list`, `act.read`, `act.do`, `act.job`. This is the one you use. |
| browser | `resumebot-browser` | `http://127.0.0.1:8530/mcp-browser/` | the queue of small browser jobs that the app's browser sessions work (find a posting, read its text). You normally never use it by hand. |

Each door has its own token, made by `resumebot init` in `secrets\` in your data folder. Both addresses are on this machine
only, and a call without the right token is answered 401. The addresses follow your port if you changed it
(`RESUMEBOT_APP_URL`). The browser door is on the engine's address, the page's port plus 100.

The tools and the rules for using them are in [`agent-guide.md`](agent-guide.md).

## Register them

```
resumebot connect
```

Add `--data "<data folder>"` if you did not set `RESUMEBOT_DATA_DIR`. The app must be running. For each door it first
asks Claude Code what it holds (`claude mcp get`). If the door is already there at this install's address, with this
install's token, it prints `already registered, and it answers` and changes nothing. Otherwise it runs
`claude mcp add --transport http --scope user <name> <address> --header "Authorization: Bearer <token>"`, which writes into
Claude Code's own configuration for your user, so the doors are there in every project. If the name is already taken by
another address or token, it removes the old door and adds the new one; if that second add fails, it puts the old door
back (or says plainly that the door is now unregistered and to run `connect` again). It then reads what Claude Code holds
back and checks it is this install's door, and asks the door for its tools to check it answers. The engine's door can take
up to 30 seconds after the app starts; `connect` waits for it and says `the engine is not up yet` if it still does not
answer. It prints the command with the token cut to its last four characters, and never prints what `claude mcp get`
says, because that holds the whole token. Running it again is safe, and it is how you repair a door or point it at a new
port.

`connect` and `doctor` both print which Claude Code they use (the first one that answers `--version`, so a broken copy on
the PATH is passed over) and which configuration file it reads: `.claude.json` in your home folder, or in
`CLAUDE_CONFIG_DIR` if that is set. A Claude Code run with a different `CLAUDE_CONFIG_DIR` reads a different file and will
not see these doors.

Claude Code reads its MCP servers when it starts. **Restart Claude Code** after `connect`.

## Check it

In the new Claude Code session, ask Claude to call `actions.list` on `resumebot-owner`. If it returns the list of actions,
the doors are connected. If it fails, run `resumebot doctor`: its two door lines say whether the app answers and whether
your Claude Code has the door registered at this install's address and with this install's token. `doctor` also checks
that the desktop shortcut exists, starts a program that exists, and opens this data folder. If something other than this
app (or an older version of it) answers at the app's address, both say so.

To see what Claude Code holds: `claude mcp list`.

## The agents' sign-in

The app's five agents (search, two for analysis, the owner's and the writer) are headless Claude Code sessions that the app
starts on your own subscription. They have no browser to sign in with, so they need a token that you make once and store.
Without it they cannot run, and `resumebot doctor` shows `MISSING agents sign in`. The app removes `ANTHROPIC_API_KEY`
from their environment: they run on your subscription and never on an API key.

```
resumebot signin
```

Claude runs this for you during the install. It runs `claude setup-token` out of sight and waits up to 10 minutes:

1. Approve once in the browser tab that opens. That is all you do.
2. It takes the token off setup-token's output itself, stores it and prints `signed in`. Nothing is typed or pasted.

If no tab opened, Claude gives you the link it printed. Open it, approve, and if that page shows a code, paste the code
into the chat: Claude runs `resumebot signin --code <code>` to hand it over (in your own terminal, paste it at the
prompt instead).

The token never passes through Claude or its output. Add `--data "<data folder>"` if you did not set
`RESUMEBOT_DATA_DIR`, and call `resumebot` by its full path if your shell does not find it (see [`install.md`](install.md)).

`signin` refuses a token cut short or with a space in it, and writes nothing in that case. It stores the token as
`secrets\claude-oauth-token`, limited to your Windows user. The token never goes through Claude's chat: your Claude is
told not to ask you for it, and you should not paste it there. It lasts about a year; if the agents stop signing in,
ask Claude to run `resumebot signin --again`.

`resumebot doctor` should now say `ok       a token is stored`.
