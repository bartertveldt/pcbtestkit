# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

Only the latest release gets fixes.

## Reporting a vulnerability

Please report security problems privately: on GitHub, open the repository's **Security** tab and click
**Report a vulnerability**. Do not open a public issue for them.

Useful to include: the version, how to reproduce it (a minimal `.kicad_pcb` / `pcbtestkit.json` if a file
triggers it), and what an attacker could do.

You can expect a first answer within a week. If the report is accepted, a fix is released and the advisory is
published with credit to you (unless you prefer otherwise); if it is declined, you get the reason.

## Scope

PCB TestKit runs locally and needs no network access. Relevant areas are:

- parsing untrusted KiCad files (`.kicad_pcb`, `.kicad_sch`, 3D models);
- the MCP server and the agent tools, which write `pcbtestkit.json` next to a project (guardrails: dry run,
  confirmation, audit log, undo);
- the Raspberry Pi / MCU test runner bundle.
