// picture_apple_album, Spectra full-bleed image. Renders one signed
// Apple Photos shared-album asset in the cell. No overlay; the
// stream's vibe is the whole point.
//
// The Scale option maps onto object-fit, like the other picture widgets.
// The shared .is-bleed rule defaults the <img> to cover, so every mode
// (including the "fill" default) is set inline here so the dropdown
// actually does something. "Fit with blurred background" paints a
// blurred, cover-scaled copy of the same image behind the contained one.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const OBJECT_FIT = {
  fit: "contain",
  fill: "cover",
  stretch: "fill",
  blurred: "contain",
};

function fmtDate(iso) {
  if (typeof iso !== "string" || !iso) return "";
  // Apple gives ISO "2024-05-12T13:45:00.123Z", slice to date only.
  return iso.slice(0, 10);
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const opts = ctx?.cell?.options || {};
  const showCaption = opts.show_caption === true; // default false, pictures are the point
  const scale = OBJECT_FIT[String(opts.scale || "fill")] ? String(opts.scale || "fill") : "fill";
  const objectFit = OBJECT_FIT[scale];
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="picture_apple_album">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>Album</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  if (!data.url) {
    shadow.innerHTML = `
      ${css}
      <div class="w is-bleed" data-widget="picture_apple_album">
        <div class="bleed-empty">No photos.</div>
      </div>`;
    return;
  }

  const captionBits = [];
  if (showCaption) {
    if (data.date) captionBits.push(fmtDate(data.date));
    if (data.owner) captionBits.push(data.owner);
    if (data.count) captionBits.push(`${data.count} photos`);
  }

  const src = escapeHtml(data.url);
  const backdrop = scale === "blurred"
    ? `<img src="${src}" alt="" aria-hidden="true" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;filter:blur(18px);transform:scale(1.1);">`
    : "";
  shadow.innerHTML = `
    ${css}
    <div class="w is-bleed" data-widget="picture_apple_album" data-scale="${scale}" style="overflow:hidden;">
      ${backdrop}
      <img src="${src}" alt="" style="position:relative;object-fit:${objectFit};">
      ${captionBits.length
        ? `<div class="img-overlay"><span class="sub">${escapeHtml(captionBits.join(" · "))}</span></div>`
        : ""}
    </div>`;
}
