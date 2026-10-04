# Dashboard local resources

These resources are generated/copied by `build-assets.cjs` from the exact npm
versions in `package.json` and `package-lock.json`. Generated files in `vendor/`
are shipped with the plugin; end users do not need npm or an external CDN.

| Package | Version | Included resources | License |
| --- | --- | --- | --- |
| Vue | 3.5.43 | Production global build, including the template compiler | MIT |
| Tailwind CSS | 3.4.17 | Static CSS generated from `index.html` and `app.js` | MIT |
| Font Awesome Free | 6.4.0 | Base/solid/regular CSS and associated webfonts | Code: MIT; Fonts: SIL OFL 1.1; Icons: CC BY 4.0 |
| Outfit (`@fontsource/outfit`) | 5.3.0 | Normal weights 300–700, Latin/Latin Extended font files | SIL OFL 1.1 |

Upstream license texts are preserved in `vendor/licenses/`, and CSS/JS copyright
headers are retained. Font Awesome is by Fonticons, Inc.; Outfit is by the Outfit
project authors. See the included license texts for the full copyright notices.
`vendor/asset-manifest.json` records the exact versions, sizes and SHA-256 hashes
of shipped assets. It is build metadata, not an additional runtime dependency.

Developer rebuild (Node.js 18+; run in this directory):

```sh
npm ci --ignore-scripts --cache ./.npm-cache
npm run build
```

Commit the source configuration, lockfile and generated `vendor/` output together.
Rebuild after changing template/JavaScript Tailwind classes or dependency versions.
The build does not modify the main project or any reminder data. Normal plugin
installation and existing Node unit tests do not need npm dependencies.

## Stylesheet precedence

Load Outfit, Font Awesome and `style.css` before `vendor/tailwind.min.css`.
The custom stylesheet defines component defaults; utilities must be able to
override their borders, radii and shadows. Preserve specific toast background
and keyboard-focus rules, and avoid hover border shorthands that reset the
left-edge status markers. Class presence alone is not a visual regression test.
See [frontend maintenance notes](../docs/FRONTEND_MAINTENANCE.md) for the checks
and the isolated `/style-probe` page. Direct `style.css` edits still need no build.
