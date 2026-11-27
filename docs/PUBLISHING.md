# Publishing

Everything is prepared; the steps below need your accounts. Placeholders use `bartertveldt`.

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

The first commit was made as `Bart Ertveldt <bartertveldt@users.noreply.github.com>` so no real email
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

1. Bump the version in `pcbtestkit/__init__.py` and `.claude-plugin/plugin.json`, and add a `## <version> (date)`
   section to `CHANGELOG.md`. The release notes are taken from that section (`tools/release_notes.py`).
2. `python3 -m unittest discover -s tests && python3 tools/gen_docs.py --check`
3. Commit, push the branch and wait for CI to pass on every OS. Then tag and push:

   ```bash
   git tag -a v1.2.0 -m "PCB TestKit 1.2.0"
   git push origin v1.2.0
   ```

The **Release** workflow runs the tests again, then builds and publishes the release:

- `pcbtestkit-<version>-kicad-pcm.zip` (reproducible: file times from the tagged commit);
- the Python wheel and sdist;
- the KiCad repository entry (`kicad-pcm-metadata.json` with the download URL, SHA-256 and sizes);
- the CycloneDX SBOM (`pcbtestkit-<version>.cdx.json`);
- `SHA256SUMS`;
- signed build provenance for every file.

Anyone can then install the zip with KiCad (*Plugin and Content Manager > Install from File...*) and check it:

```bash
gh attestation verify pcbtestkit-1.2.0-kicad-pcm.zip --repo bartertveldt/pcbtestkit
```

**A failed release build.** Never move or reuse a tag that has been pushed. Fix the problem on the branch, bump the
version, and tag again. Mark the failed version in `CHANGELOG.md` as `(tagged <date>, not published: part of
<next>)`, and its changes go into the next release's notes.

**Security fixes.** Prepare the fix in the advisory's private fork (Security tab > the advisory > *Start a temporary
private fork*). Release it as above, then publish the advisory (see [SECURITY.md](../SECURITY.md)).

## 5. KiCad Plugin and Content Manager (official repository)

### 5a. Test the release on other machines first

Install the GitHub release zip the way users will, on as many setups as you can (Windows, Linux, macOS;
KiCad 8, 9 and 10):

1. Download `pcbtestkit-<version>-kicad-pcm.zip` from the release page.
2. KiCad: **Plugin and Content Manager > Install from File...**, choose the zip, then in the PCB Editor
   **Tools > External Plugins > Refresh Plugins**. The PCB TestKit button appears on the toolbar.
3. Open a board (the KiCad demo `pic_programmer` is a good one), click the button, press **Generate** with the
   defaults, and check that the report and the guide open and the files are in `production_test/`.
4. Try each tab without AI: the three fixture modes, a rail voltage edit, a part height, accepting a weak point.
5. Open the jig STLs in your slicer, and the fixture `.kicad_pcb` in KiCad.
6. Installing the next version over the old one (Install from File again) must keep `pcbtestkit.json`.

Note what you tested; it goes into the merge request. Problems go to the issue tracker.

### 5b. Prepare the merge request (nothing is sent)

```bash
python3 tools/build_pcm.py --submission v1.0.0
```

It downloads the release's zip and `kicad-pcm-metadata.json` from GitHub and checks them against KiCad's rules:
- the SHA-256 and size match the published zip;
- the archive holds only `metadata.json`, `plugins/` and `resources/`;
- the zip's own metadata has exactly this one version and no download fields;
- the schema rules hold.

It keeps any versions KiCad already lists, so for an update the new version is added on top. It writes:

- `dist/kicad-submission/packages/com.github.<owner>.pcbtestkit/metadata.json` and `icon.png`
- `dist/kicad-submission/MERGE_REQUEST.md`: title and description; fill in the "Tested on" line.

### 5c. Submit

1. Create a GitLab account, fork https://gitlab.com/kicad/addons/metadata, clone the fork and make a **new
   branch** (KiCad's validation does not work as intended on `main`).
2. Copy `dist/kicad-submission/packages/<identifier>/` into the fork's `packages/` and push the branch.
3. The fork's pipeline validates the metadata and builds a temporary PCM repository (link in the build job's
   log): add it in KiCad (**Plugin and Content Manager > Manage...**) to test the listing itself.
4. Open the merge request with the text from `MERGE_REQUEST.md`. After review and merge, the plugin appears in
   every KiCad's Plugin and Content Manager within about a day.
5. Updates: release the new version on GitHub, run step 5b with its tag, and send a new merge request.

Requirements met by this repository:
- OSI licence (MIT, GPL-compatible), English metadata, a public issue tracker;
- a 64x64 icon, a description under 150 characters;
- identifier `com.github.<owner>.pcbtestkit` (permanent once listed);
- no download fields inside the package's own `metadata.json`.

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

- [x] `tools/set_owner.py` run: `git grep YOUR-GITHUB` finds only the two tools that detect the placeholder
- [x] commit author set to your noreply address
- [x] CI green
- [x] tag pushed, release has the PCM zip, wheel and `metadata.json`
- [ ] release has `SHA256SUMS`, the SBOM and attestations (`gh attestation verify` passes)
- [ ] repository settings: private vulnerability reporting, secret scanning with push protection, Dependabot
      alerts, and branch protection on `main` (require CI to pass)
- [ ] release zip installed and used on other machines and KiCad versions (5a)
- [ ] `tools/build_pcm.py --submission <tag>` passes (5b)
- [ ] merge request to gitlab.com/kicad/addons/metadata (5c)
