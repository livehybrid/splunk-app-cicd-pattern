# Splunk App CI/CD Pattern

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

One file drives all of it: [`.github/workflows/splunk-app-ci.yml`](.github/workflows/splunk-app-ci.yml).
The test and ship stages are reusable workflows kept in
[`livehybrid/deploy-splunk-app-action`](https://github.com/livehybrid/deploy-splunk-app-action),
so every add-on you own gets the same gates from one place.

## Use it for your own add-on

1. Clone or use this repo as a template.
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

## What this repo is not

It does not deploy to a Splunk stack. Installing onto a real Cloud-shaped
Splunk is a separate, optional stage and deliberately not part of this pattern:
the build, vet and ship legs above need no Splunk instance, no credentials and
no allowlisted egress IP, which is what makes them safe to copy as-is.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
