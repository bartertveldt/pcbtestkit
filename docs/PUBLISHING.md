# Publishing

Everything is prepared; the steps below need your accounts. Placeholders use `YOUR-GITHUB-USER`.

## 1. Fill in the GitHub owner (once)

```bash
python3 tools/set_owner.py --owner <github-user> --dry-run     # list the files
python3 tools/set_owner.py --owner <github-user>
python3 tools/build_pcm.py --check
```

This sets URLs everywhere and the KiCad package identifier `com.github.<github-user>.pcbtestkit`.
**The identifier is permanent** once a version is in KiCad's repository; use the account the repository will
stay under (a personal account or an organisation).

## 2. Commit author

The first commit was made as `Bart Ertveldt <YOUR-GITHUB-USER@users.noreply.github.com>` so no real email
address is published. Before pushing, set your own (GitHub's private address is under
*Settings > Emails*, e.g. `12345678+user@users.noreply.github.com`) and rewrite the author:

```bash
git config user.email "<your github noreply address>"
git rebase --root --exec "git commit --amend --no-edit --reset-author"
```

## 3. Push to GitHub

Create an empty repository named `pcbtestkit` (no README or license; they exist here), then:

```bash
git remote add origin https://github.com/<github-user>/pcbtestkit.git
git push -u origin main
```

The **CI** workflow runs the tests on Linux, macOS and Windows with Python 3.9 and 3.13, checks that the
generated docs are current, validates the class systems and the KiCad package, and builds the package.

Suggested repository settings: description "KiCad production-test toolkit: DFM, fab classes, test fixture,
solder jig, AI/MCP"; topics `kicad`, `kicad-plugin`, `pcb`, `dfm`, `test-fixture`, `mcp-server`, `claude-code-plugin`;
the social preview image `docs/media/social-preview.png`.

## 4. Release

1. Bump the version in `pcbtestkit/__init__.py` and `.claude-plugin/plugin.json`; add a `CHANGELOG.md` section.
2. `python3 -m unittest discover -s tests && python3 tools/gen_docs.py --check`
3. Commit, then tag and push:

   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

The **Release** workflow builds `pcbtestkit-<version>-kicad-pcm.zip` and the Python wheel, and attaches them
plus the KiCad repository entry (`metadata.json` with the download URL, SHA-256 and sizes) to a GitHub release.
Anyone can then install the zip with KiCad: *Plugin and Content Manager > Install from File...*.

## 5. KiCad Plugin and Content Manager (official repository)

1. Create a GitLab account and fork https://gitlab.com/kicad/addons/metadata.
2. Copy the release asset `metadata.json` (and `icon.png` from `dist/pcm-repository/packages/<identifier>/`
   after `python3 tools/build_pcm.py`) to `packages/<identifier>/` in the fork.
   The `download_url` must point at the public release asset; the SHA-256 must match it.
3. Open a merge request. KiCad's maintainers review it; after merging, the plugin shows up in every KiCad's
   Plugin and Content Manager.
4. For updates, add a new entry to `versions` (keep the older ones) in a new merge request.

Requirements met by this repository: OSI license (MIT, GPL-compatible), English metadata, public issue tracker,
64x64 icon, description under 150 characters, no download fields inside the package's own `metadata.json`.

## 6. Claude Code plugin

Nothing to submit: the repository is its own marketplace. Users run

```text
/plugin marketplace add <github-user>/pcbtestkit
/plugin install pcbtestkit@pcbtestkit
```

If you have Claude Code, check the manifests first with `claude plugin validate .`. Optionally list the plugin
in community marketplaces or directories.

## 7. Python package and MCP registry (optional)

```bash
python3 -m pip install build twine
python3 -m build                      # dist/pcbtestkit-<v>.tar.gz and .whl
python3 -m twine upload dist/pcbtestkit-*
```

With the package on PyPI, `pip install pcbtestkit` gives the `pcbtestkit` and `pcbtestkit-mcp` commands, and the
server can be listed in the MCP registry (https://registry.modelcontextprotocol.io) with a `server.json` that
points at the PyPI package. Check that the name `pcbtestkit` is still free on PyPI first.

## Checklist

- [ ] `tools/set_owner.py` run, no `YOUR-GITHUB-USER` left (`grep -r YOUR-GITHUB-USER .`)
- [ ] commit author set to your noreply address
- [ ] CI green
- [ ] tag pushed, release has the PCM zip, wheel and `metadata.json`
- [ ] zip installed once through KiCad's *Install from File...* and the toolbar button works
- [ ] merge request to gitlab.com/kicad/addons/metadata
