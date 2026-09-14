# Splunk App CI/CD Pattern

[![Splunk App CI/CD](https://github.com/livehybrid/splunk-app-cicd-pattern/actions/workflows/splunk-app-ci.yml/badge.svg)](https://github.com/livehybrid/splunk-app-cicd-pattern/actions/workflows/splunk-app-ci.yml)

A minimal Splunk add-on wired to a working build, test and ship pipeline.

The add-on is deliberately trivial: one custom search command that returns a
single event. **The pipeline is the point.** Clone this, swap the add-on for
yours, and every change builds, gets vetted and ships itself.

Presented at Splunk .conf26 in **DEV1036 — Build, Test, and Ship**
(Will Searle, Octamis / SplunkTrust · Ben Lovley, Splunk).

## The pattern

```
  Commit / PR  ->  ucc-gen build  ->  AppInspect  ->  package  ->  publish
                                     cloud+future     + commit     release /
                                                        hash       Splunkbase
```

Every step is something you already do by hand. All that changes is that it now
happens the same way, every time, without you.

| Stage | What runs | When |
|-------|-----------|------|
| **Build** | `ucc-gen build` against `package/` | every push and PR |
| **Test** | AppInspect CLI, tags `cloud,future,private_victoria` | every push and PR |
| **Test (deep)** | AppInspect API, the same vetting Splunkbase runs | opt-in, see below |
| **Package** | tarball stamped `<version>+<short commit hash>` | every push and PR |
| **Ship** | GitHub release with the tarball attached | only on a `v*.*.*` tag |

Publishing to Splunkbase is deliberately not wired up here. The pipeline's job
is to hand you a vetted, versioned tarball; uploading it stays a human step, which
keeps a person in the loop on what reaches customers.

One file drives all of it: [`.github/workflows/splunk-app-ci.yml`](.github/workflows/splunk-app-ci.yml).
The test and ship stages are reusable workflows kept in
[`livehybrid/deploy-splunk-app-action`](https://github.com/livehybrid/deploy-splunk-app-action),
so every add-on you own gets the same gates from one place.

## Use it for your own add-on

1. Click **Use this template**, or clone it.
2. Replace `package/` and `globalConfig.json` with your add-on.
3. Search and replace `TA-cicd-pattern` with your add-on's name (it appears in
   the workflow's build and tarball steps).
4. Push. The build and AppInspect legs work immediately, with no credentials.
5. Tag `v1.0.0` when you want a release.

### Optional: the AppInspect API leg

The API leg runs the same vetting Splunkbase runs, which needs Splunkbase
credentials. It is off by default so a fresh clone stays green. To enable it:

- add repository secrets `SPLUNKBASE_USERNAME` and `SPLUNKBASE_PASSWORD`
- add repository **variable** `APPINSPECT_API` = `true`

Left unset, that job skips and the rest of the pipeline is unaffected.

### Optional: the SCTS leg (install on a real Splunk)

AppInspect reads your package. It cannot tell you whether the add-on actually
loads. The [Splunk Cloud Testing Service](https://scts.dev.splunk.com) hands out
short-lived Splunk stacks from an API, which closes that gap:

```
build -> request a stack -> wait for RUNNING -> install -> verify the version -> delete
```

The verify step is the one that earns its keep. Because the build stamps the
commit hash into `app.conf`, reading the version back off the stack proves
*which commit* is running, not merely that something with the right name
installed.

Off by default, like the API leg. To enable it:

- add repository secret **`SCTS_API_TOKEN`** = your Dev Portal API token
- add repository **variable** `SCTS_ENABLED` = `true`

Then run it from the Actions tab (**SCTS stack test** → Run workflow), where you
can pick a Splunk version or tick `keep_stack` to leave the stack up and poke at
it by hand.

The token is a **secret**, never a file in the repo. Everything derived from a
stack's credentials is passed through `::add-mask::` before it can reach a log
line: the stack is short-lived, but a build log is not. For the same reason this
workflow never triggers on `pull_request`, so a fork cannot spend your stack
quota, and never on `pull_request_target`, which would hand a fork your token.

Teardown runs under `if: always()`, so a failed install still deletes the stack.

The client is [`.github/scripts/scts.py`](.github/scripts/scts.py), stdlib only,
and it works from your laptop too:

```bash
export SCTS_API_TOKEN=...
python3 .github/scripts/scts.py versions
python3 .github/scripts/scts.py create
python3 .github/scripts/scts.py install <stack-id> --package dist/TA-cicd-pattern-1.0.0+abc1234.tar.gz --app-name TA-cicd-pattern
python3 .github/scripts/scts.py delete <stack-id>
```

### Run the same checks locally

```bash
pip install splunk-appinspect
ucc-gen build --source package --ta-version 1.0.0
splunk-appinspect inspect output/TA-cicd-pattern --included-tags cloud,future
```

Same tags as CI, so a local pass means a CI pass.

## Things worth knowing

Four things that cost everyone a red build once:

- **`included-tags` is where the value is.** `cloud` answers "will this run on
  Splunk Cloud?". `future` answers "what is about to break?". The `future` tag is
  the one people miss, and it is the one that warns you about a platform Python
  move before it reaches a customer.
- **AppInspect rejects group-writable files.** 775 and 664 fail the check. The
  build normalises to 755 for directories and 644 for files before packaging.
- **Pin your Python.** The build pins the runner to 3.11 and
  `package/default/commands.conf` pins `python.version = python3`. A platform
  bump should be a decision, not a surprise.
- **Gate publishing on a tag.** The release job is conditioned on
  `refs/tags/v*`, so a stray commit to a branch cannot ship an add-on.
- **`ucc-gen build` rewrites `globalConfig.json` in place**, stamping in the
  version you passed. Harmless in CI on a throwaway checkout, but a local
  build will leave your working copy dirty. Do not commit that.

## What this repo is not

It does not deploy to a Splunk stack. Installing onto a real Cloud-shaped
Splunk is a separate, optional stage and deliberately not part of this pattern:
the build, vet and ship legs above need no Splunk instance, no credentials and
no allowlisted egress IP, which is what makes them safe to copy as-is.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
