# YBM brand mark

`ybm-mark.svg` is the canonical source for the chunky Y used by YBM.
`ybm-mark.png` is the transparent 1024 px raster export.

- Primary color: `#6274E8`
- Canvas: transparent, square
- Construction: one flat, rounded stroke with no effects or internal detail

`social-preview.png` is the 1280x640 card GitHub shows when the repository is linked. GitHub has no
API for it: upload the file under Settings, General, Social preview. `social-preview.html` is its
source (the tagline and the five stages are claims about the product, so keep them in line with the
README). Render it with
`npx playwright screenshot --viewport-size=1280,640 docs/brand/social-preview.html docs/brand/social-preview.png`.

Consumer copies are kept where their build or packaging boundary requires them:

- `frontend/public/favicon.svg` for the browser tab and admin-console shell
- `scripts/assets/logo_256.png` for the Windows tray
- `vscode-extension/images/icon.png` for the VS Code extension package

The raster files are generated from the same geometry and should remain visually
identical to this source.
