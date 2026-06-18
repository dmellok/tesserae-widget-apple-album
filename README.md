# iCloud Shared Albums widget for Tesserae

Photo carousel from a public [iCloud Shared Album](https://support.apple.com/guide/photos/share-photos-mac-pht43e0d6c7f/mac) link, rendered full-bleed onto an e-ink panel. For [Tesserae](https://github.com/dmellok/tesserae), the e-ink dashboard companion.

## How it works

1. Open the album in Photos (Mac, iPad, iPhone, or icloud.com).
2. Enable **Public Website** on the album.
3. Copy the share link (`https://www.icloud.com/sharedalbum/#B0xxxxxx`) or just the token (`B0xxxxxx`).
4. Paste it into the widget's cell options.

No API key needed. The widget uses Apple's reverse-engineered iCloud Shared Album endpoints, undocumented but stable for ~10 years.

## Install

Settings → Widgets → Browse community widgets → Install.

## Folders shipped

- `picture_apple_album`

## License


AGPL-3.0-or-later. See [LICENSE](LICENSE).

