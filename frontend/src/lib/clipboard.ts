/**
 * Copy text to the clipboard.
 *
 * `navigator.clipboard` only exists in a secure context. DistillPod is
 * self-hosted, so it is often reached over plain http on a LAN IP where the
 * API is simply undefined — hence the execCommand fallback, which still works
 * there. Returns false if both routes fail, so callers can show an error
 * instead of a success tick.
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permission denied or transient failure — fall through.
    }
  }

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Keep it off-screen and non-focusable-looking, but still selectable.
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/** Trigger a browser download of `text` as a file. */
export function downloadText(filename: string, text: string, mime = "text/markdown;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Filename-safe slug: strips accents, punctuation and repeated dashes. */
export function slugify(s: string, max = 60): string {
  return (
    s
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")   // drop combining accents
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, max)
      .replace(/-+$/, "") || "chat"
  );
}
