# Platmark

The public website and layer catalogue for Platmark. The app's source is private.

## Website

Published with GitHub Pages from the root of `main`:

- https://ouroborostechnologies.github.io/platmark/support/
- https://ouroborostechnologies.github.io/platmark/privacy/

These URLs are compiled into the app (`Constants.swift`) and listed on the App Store, so keep the paths stable.

## Layer catalogue

The catalogue files go in `catalogue/`, one per schema version (`catalogue-v1.json`, `catalogue-v2.json`, …). To publish them, tag the commit and push the tag:

```sh
git tag catalogue-2026.10.26
git push origin catalogue-2026.10.26
```

`.github/workflows/release-catalogue.yml` then creates a release of that name with every `catalogue/*.json` attached. Apps fetch `https://github.com/ouroborostechnologies/platmark/releases/latest/download/catalogue-v<N>.json`.

A tag on a commit that isn't on `main` becomes a pre-release. `latest` skips pre-releases, so a branch's catalogue never reaches users; debug builds can fetch it by tag from `releases/download/<tag>/…`.

Releases in this repo are only for the catalogue. Any other release would become `latest` and break the address above.
