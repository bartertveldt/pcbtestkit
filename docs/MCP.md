# MCP server

`pcbtestkit mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server (stdio, JSON-RPC 2.0,
protocol 2024-11-05 to 2025-06-18). It needs only Python 3.9+; no SDK or extra packages.

```bash
pcbtestkit mcp --project /path/to/board.kicad_pro [--profile core|full|readonly] [--read-only]
python3 -m pcbtestkit mcp ...        # without pip: PYTHONPATH=/path/to/pcbtestkit-repo
pcbtestkit-mcp ...                   # same, as its own console script
```

`--project` may be a `.kicad_pcb`, `.kicad_pro` or folder. If it can't be opened the server still starts and
the model is told to call `open_project`.

## Which way to connect (cheapest first)

| Client | Recommended | Why |
|---|---|---|
| Claude Code | The plugin (skill + MCP `core`), or only the skill | The skill drives the CLI: no tool definitions in context until used. |
| Any agent with a shell (Codex, Cursor agent, Gemini CLI, Aider, ...) | `AGENTS.md` + `pcbtestkit guide` | Same: zero standing token cost. |
| Chat apps without a shell (Claude Desktop, ...) | MCP with `--profile core` | 16 tools, about 3k tokens of definitions per request. |
| Full control / tool authoring | MCP `--profile full` | All 33 tools, about 6k tokens. |
| Review only | MCP `--profile readonly` or `--read-only` | No writes possible. |

## Client setup

Replace `/path/to/project` with the KiCad project. If you didn't `pip install`, use
`"command": "python3", "args": ["-m", "pcbtestkit", "mcp", ...]` and add `"env": {"PYTHONPATH": "/path/to/pcbtestkit-repo"}`.

**Claude Code: plugin (skill + server)**

```text
/plugin marketplace add bartertveldt/pcbtestkit
/plugin install pcbtestkit@pcbtestkit
```

The plugin starts the server for the project you have open in Claude Code (`${CLAUDE_PROJECT_DIR}`) with
`--profile core`. Change it in `.claude-plugin/plugin.json`.

**Claude Code: server only**

```bash
claude mcp add pcbtestkit -- pcbtestkit mcp --project /path/to/project --profile core
# without pip:
claude mcp add pcbtestkit --env PYTHONPATH=/path/to/pcbtestkit-repo -- python3 -m pcbtestkit mcp --project /path/to/project --profile core
```

**Claude Desktop** (`claude_desktop_config.json`), **Cursor** (`.cursor/mcp.json`), **Windsurf** and most others:

```json
{"mcpServers": {"pcbtestkit": {"command": "pcbtestkit",
  "args": ["mcp", "--project", "/path/to/project", "--profile", "core"]}}}
```

**VS Code** (`.vscode/mcp.json`):

```json
{"servers": {"pcbtestkit": {"type": "stdio", "command": "pcbtestkit",
  "args": ["mcp", "--project", "${workspaceFolder}", "--profile", "core"]}}}
```

**OpenAI Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.pcbtestkit]
command = "pcbtestkit"
args = ["mcp", "--project", "/path/to/project", "--profile", "core"]
```

**KiCad dialog.** The plugin's **AI assistant** tab shows ready-to-copy snippets with your paths filled in.

## What the server offers

**Tools.** The agent API: see [reference/tools.md](reference/tools.md). Every tool has a title and MCP
annotations (`readOnlyHint`, `destructiveHint` for `generate_outputs` and `add_class_system`, `idempotentHint`).
Results come back once, as compact JSON text with `"ok"`; errors carry a code, a message and usually a hint or
`did_you_mean`. Writes support `dry_run`, `confirm`, a rationale, an audit log and `undo_last` (20 steps, kept
on disk next to the project).

**Resources.**

| URI | Content |
|---|---|
| `pcbtestkit://guide` | The workflow in 10 lines |
| `pcbtestkit://cli` | CLI quick reference with token rules (`pcbtestkit guide`) |
| `pcbtestkit://config/defaults` | Every `pcbtestkit.json` key with its default |
| `pcbtestkit://class-systems` | id, name, kind, status and classes of each fab class system |
| `pcbtestkit://class-systems/catalog` | Metrics, aliases and context keys for writing class systems |
| `pcbtestkit://class-systems/{id}` | One class system's JSON definition (template) |
| `pcbtestkit://project/overview` | `get_overview` of the open board (listed when a board is open) |

**Prompts.**

| Prompt | Arguments | Does |
|---|---|---|
| `quick_dfm_check` | none | Two tool calls: overview + top findings. The cheapest first look. |
| `review_weak_points` | `limit` | Critical/major findings with one concrete layout fix each |
| `choose_fab_class` | `system`, `target` | Which fab classes fit, what blocks the others; stores the choice |
| `plan_production_test` | `video` | The whole decision workflow, then generate outputs |
| `generate_outputs` | `video`, `out_dir` | Validate, then write all files |
| `add_fab_class_system` | `source` (required) | Turns a fab's design-rule page into a validated class system |

**Completion.** `completion/complete` suggests class-system ids for `system` / `{id}` arguments.

## Saving tokens

- Use `--profile core` unless the model needs jig, fixture or class-system authoring tools.
- Start with `quick_dfm_check` or `get_overview`; most questions need two or three calls.
- Pass `limit`, `min_severity`, `max_items` and `offset`; read generated files with `read_output` + a JSON pointer.
- Writes report their `effects`; there is no need to re-read the plan after each decision.

## Testing the server

```bash
npx @modelcontextprotocol/inspector pcbtestkit mcp --project /path/to/project
```

or by hand:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_overview","arguments":{}}}' \
  | pcbtestkit mcp --project /path/to/project
```
