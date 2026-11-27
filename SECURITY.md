# Security policy

PCB TestKit is a KiCad plugin, a command-line tool, an MCP server for AI agents and a test runner. It reads KiCad
files that may come from someone else, writes files that people share, and drives test hardware. This page explains
how to report a vulnerability, what happens after you report one, what is in scope, and how the project is built and
released.

## Supported versions

| Version | Security fixes |
| ------- | -------------- |
| 1.2.x   | Yes: every fix is released as a new 1.2.x version. |
| 1.1.x and older | No. Please update to the latest release (in KiCad: *Plugin and Content Manager > Update*). |

Fixes are free of charge, and they ship as normal releases through the KiCad Plugin and Content Manager, GitHub
Releases and the Python package.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately instead:

1. Open <https://github.com/bartertveldt/pcbtestkit/security/advisories/new>. You can also go to the repository's
   **Security** tab and click **Report a vulnerability**. Only the maintainers can read the report.
2. Describe the problem:
   - the version (`pcbtestkit --version`, or the version shown in KiCad's Plugin and Content Manager), KiCad and the
     operating system;
   - how to reproduce it: the smallest `.kicad_pcb`, `.kicad_sch`, `pcbtestkit.json`, facility or 3D model that
     triggers it;
   - what an attacker could do with it, and what they would need to do first (for example: get someone to open a
     board, to run `pcbtestkit run` on it, or to connect an AI agent to it).

Please send a minimal file that you made yourself, not a customer's design. If you cannot share a file, describe it.

If you have no GitHub account, open a public issue that only asks for a private contact. Put no details in it.

## What happens next

| Step | Target |
| ---- | ------ |
| You get a first answer | within 5 working days |
| The report is assessed (accepted or declined, with the reason) and rated with CVSS | within 10 working days |
| A fix is released: critical / high | within 30 days |
| A fix is released: medium / low | within 90 days, or in the next release |

These are targets, not guarantees: PCB TestKit is maintained by volunteers. You hear from us at every step. If a fix
will take longer, we say why and agree a new date with you.

For an accepted report, we:

- fix it in a private fork, add a test, and release the fix as a new version;
- publish a [GitHub Security Advisory](https://github.com/bartertveldt/pcbtestkit/security/advisories). It says
  which versions are affected, what to do, and gives a CVE identifier (GitHub assigns these) when the problem
  warrants one;
- credit you by name in the advisory and the changelog, unless you would rather not be named.

We publish the advisory when the fix is out. We follow coordinated disclosure: please give us up to **90 days**
before you publish details yourself. If the problem is already being exploited, we publish sooner and agree the
timing with you.

If someone else made the vulnerable component (KiCad, kicad-cli, ffmpeg, Python), we pass the report on to them,
with your permission.

## Safe harbour

We will not take legal action against you, or ask anyone else to, for security research that is done in good faith
and stays within this policy. Good faith means:

- you test only on your own installation, with your own files;
- you never access, change or delete data that is not yours;
- you do nothing that harms other people or their systems (no denial of service, no social engineering);
- you report the problem to us privately and give us reasonable time to fix it.

If something you plan to do is not covered here, ask us first in a private report.

## Scope

In scope: everything in this repository, and the packages built from it (the KiCad Plugin and Content Manager zip,
the Python wheel and sdist, the runner folder that a build copies into every output).

These are especially worth looking at:

| Area | What could go wrong |
| ---- | ------------------- |
| Reading KiCad files (`.kicad_pcb`, `.kicad_sch`, `.kicad_pro`), 3D models (VRML, GLB from kicad-cli) and `pcbtestkit.json` | Reading a crafted file runs code, writes outside the output folder, or hangs or crashes the KiCad session it runs in |
| The generated pages (guide, report, station page, test environment page) | Text from a board (net names, values, title block) runs as script, or the page loads something from the network |
| The MCP server and the agent tools | A tool writes outside the project folder, goes around the guardrails (dry run, confirmation, audit log, undo), or runs a program |
| The test runner (`runner/`) | A test plan runs a program without `--allow-commands`, or a field of the plan reaches a shell |
| Release artifacts | A published file doesn't match the tagged source, or the checksums, attestations or SBOM are wrong |

Out of scope:

- Facility drivers (`driver.py` next to a facility) and `command` steps in a test plan. These are code and commands
  that you choose to run on your tester, so only use the ones you trust.
- Problems that need someone to edit PCB TestKit's own installed files, or that need an attacker who already
  controls your account.
- Vulnerabilities in KiCad, Python, ffmpeg or your operating system. Report those to their own projects.
- Missing hardening that has no practical impact, and reports produced only by automated scanners.

## How PCB TestKit protects you

- **No network, no telemetry.** PCB TestKit makes no network connections and collects no data. The exceptions are
  the ones you set up yourself: a facility driver that talks to your tester, and maintainer tools such as
  `tools/build_pcm.py --submission`. See [PRIVACY.md](PRIVACY.md).
- **Generated pages are self-contained and locked down.** Every page carries a Content-Security-Policy that blocks
  everything from the network, frames, forms and plugins. The only script allowed is the page's own, by its SHA-256
  hash (it sorts and filters tables). Every text that comes from a board, a schematic or a setting is HTML-escaped,
  and a test checks this with injected HTML.
- **Untrusted files are only parsed.** PCB TestKit never evaluates or unpickles what it reads. KiCad files are read
  by an S-expression parser, and formulas in class-system files by a small, restricted arithmetic evaluator (no
  Python `eval`).
- **No shell.** External programs (kicad-cli, ffmpeg, KiCad's Python) run from argument lists, never through a
  shell, and with time limits. The runner runs test-plan commands only with `--allow-commands`, and also from
  argument lists.
- **Writes have guardrails.** Agent tools write only through one code path. That path supports a dry run, asks for
  confirmation on risky changes, keeps an audit log in `pcbtestkit.json` and keeps a persistent undo history.
- **Shared files hold no local paths.** Generated files name other files relative to the output folder. Tests check
  that no build writes the user's home folder or temporary folders into its shared files.
- **No dependencies.** The plugin, the CLI and the MCP server use only Python's standard library, so there is no
  third-party code to compromise.

## Supply chain and releases

- **Releases are built in public.** GitHub Actions builds every release from its version tag, after running the full
  test suite. Nobody uploads a release from their own computer.
- **Pinned and least-privilege workflows.** Every GitHub Action is pinned to a full commit SHA, and Dependabot
  proposes updates to them. Workflows run with read-only tokens by default, and the release job alone may write.
  Checkouts don't keep credentials.
- **Checksums, SBOM and provenance.** Every release includes `SHA256SUMS`, a CycloneDX SBOM (`pcbtestkit-<version>.cdx.json`)
  and signed build provenance (SLSA, through GitHub artifact attestations and Sigstore) for every file. The KiCad
  Plugin and Content Manager also checks the zip's SHA-256 before installing it.
- **Code scanning.** CodeQL scans every push and pull request. Secret scanning with push protection is on for the
  repository.

### Checking a download

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

```bash
gh attestation verify pcbtestkit-1.2.0-kicad-pcm.zip --repo bartertveldt/pcbtestkit
```

The second command proves that the file was built by this repository's release workflow from the tagged source.

## Laws and standards

PCB TestKit is free, open-source software that is developed and given away outside any commercial activity. It
processes no personal data of its users on any server, because it has none (see [PRIVACY.md](PRIVACY.md)). The
project still follows these good practices:

- **Coordinated vulnerability disclosure** as described in ISO/IEC 29147 and ISO/IEC 30111: one private reporting
  channel, acknowledged reports, assessment, fix, advisory, credit.
- The **EU Cyber Resilience Act (Regulation (EU) 2024/2847)**. Its manufacturer obligations do not apply to free
  software that is not placed on the market in the course of a commercial activity. The project follows the
  practices the Act expects of open-source projects anyway: this published vulnerability-handling policy, an SBOM
  for every release, free security updates, and advisories for fixed vulnerabilities.
  If you ship PCB TestKit, or its outputs, inside a product of your own, the obligations for that product are yours.
  The SBOM and the advisories are there to help you meet them.
- **Secure development** as in the OpenSSF Scorecard checks and SLSA build provenance: pinned dependencies,
  least-privilege tokens, code scanning, signed provenance.

This section describes how the project works. It is not legal advice.

## Contact

For security problems, use only the private report described above. For everything else, open an
[issue](https://github.com/bartertveldt/pcbtestkit/issues).
