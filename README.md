# iCloud Shared Albums widget for Tesserae

Photo carousel from a public [iCloud Shared Album](https://support.apple.com/guide/photos/share-photos-mac-pht43e0d6c7f/mac) link, rendered full-bleed onto an e-ink panel. For [Tesserae](https://github.com/dmellok/tesserae), the e-ink dashboard companion.

## How it works

1. Open the album in Photos (Mac, iPad, iPhone, or icloud.com).
2. Turn on the album's public link (**Public Website** on older shared albums).
3. Copy the share link, or just its token, and paste it into the widget's cell options. Accepted:
   - `https://photos.icloud.com/shared/album/<token>/` (current shared albums)
   - `https://share.icloud.com/photos/<token>` and `https://www.icloud.com/photos/#<token>`
   - `https://www.icloud.com/sharedalbum/#<token>` (older shared albums)
   - a bare token; the widget works out which kind of album it is.

No API key or Apple ID needed. Each photo uses the smallest of Apple's JPEG sizes (with the owner's edits) that covers the cell it is drawn in, so an e-paper tag gets the 2048 px JPEG and a 4K screen a bigger one; videos are skipped.

The widget uses Apple's undocumented endpoints, the same ones icloud.com uses for people without an Apple ID:

- Current shared albums are CloudKit shares. The widget resolves the token (`records/resolve`), which returns a short-lived anonymous access token, lists the album's photos (`records/query`, paged), and refreshes Apple's 15-minute download links with `records/lookup` when they run out.
- Older shared albums use the "sharedstreams" API (`webstream`, `webasseturls`), stable for ~10 years.

The photo list is cached for 6 hours, so new photos show up within that time.

On the Lineups page the widget lists the album's photos (from the cached list, with small previews), so the arrows step through them and *Show chosen* pins one through the **Pinned photo** option; a pinned photo never moves the sequential position. The widget declares `"lineups": {"pin_option": "pinned_asset", "noun": "photos", "draw": true}`. For CloudKit albums a preview is left out once Apple's 15-minute link has run out.

## Install

Settings → Widgets → Browse community widgets → Install.

## Folders shipped

- `picture_apple_album`

## Tests

The tests run against a checkout of Tesserae `main`: copy `picture_apple_album` into its `plugins/` folder (a copy, not a symlink: the widget takes its id from its folder name) and run `python -m pytest plugins/picture_apple_album/tests` from the checkout root. They never touch the network; the CloudKit tests use recorded, sanitised responses in `tests/fixtures`. To also run against a real public album:

```sh
APPLE_ALBUM_LIVE_TOKEN='https://photos.icloud.com/shared/album/<token>/' python -m pytest plugins/picture_apple_album/tests -k live
```

## License


AGPL-3.0-or-later. See [LICENSE](LICENSE).

