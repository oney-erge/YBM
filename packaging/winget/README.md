# winget manifests

Manifest templates (schema 1.12.0, three-file form - the community repository
rejects singleton manifests) for listing YBM in the [Windows Package Manager
Community Repository][winget-pkgs], so that installing is:

```
winget install YBM
```

That path shows no SmartScreen prompt and involves no download decision, which
is why it is worth having even though the installer works standalone.

## Prerequisite

winget accepts MSIX, MSI, APPX, and executable installers. It does **not**
accept script-based installers, so the MSI built by
`.github/workflows/release.yml` is what makes a listing possible at all.

A release must already be published, because the manifest pins the installer's
URL and SHA256.

## Filling in a version

The three files here carry placeholders (`__VERSION__`, `__SHA256__`,
`__RELEASE_DATE__`). Render them for a published release with:

```powershell
$version = "0.1.2"
.\scripts\render_winget_manifests.ps1 -Version $version
```

That downloads the published `YBM-Setup.msi`, hashes it, and writes the
completed manifests to `dist\winget\manifests\o\oney-erge\YBM\<version>\`.
Hashing the actual published asset is deliberate: a manifest whose SHA does not
match what users download fails validation, and copying the number by hand from
a build log is exactly how that happens.

## Submitting

Validate locally first, against the real machine:

```powershell
$version = "0.1.2"
winget validate --manifest "dist\winget\manifests\o\oney-erge\YBM\$version"
winget install --manifest "dist\winget\manifests\o\oney-erge\YBM\$version"
```

Then open a pull request against [microsoft/winget-pkgs][winget-pkgs] with the
directory placed at the same path inside that repository. Submission is free and
needs no code-signing certificate.

For the first submission, use the rendered manifests and pull-request flow above.
Alternatively, run `wingetcreate new` and answer its interactive prompts; its final
prompt can submit the new package. After `oney-erge.YBM` has been accepted,
[wingetcreate][wingetcreate] is the easier route for subsequent version bumps:

```powershell
wingetcreate update oney-erge.YBM --version 0.1.2 `
  --urls https://github.com/oney-erge/YBM/releases/download/v0.1.2/YBM-Setup.msi `
  --submit
```

## Not automated on purpose

Publishing to a public package index is an external write, and the repository's
rule is that those need explicit intent rather than happening as a side effect
of pushing a tag. The release workflow builds and hashes the installer; a person
decides when a version goes to winget.

[winget-pkgs]: https://github.com/microsoft/winget-pkgs
[wingetcreate]: https://github.com/microsoft/winget-create
