/**
 * Puts `text` on the clipboard; resolves true when it got there.
 *
 * The Clipboard API only exists on a secure page (https, or localhost). The hub opened
 * over plain http from another device - a phone on the same network - falls back to
 * the older copy command on a hidden text area, which those browsers still honour.
 */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Refused (no permission, the page lost focus): try the older way.
    }
  }
  return copyWithTextArea(text);
}

function copyWithTextArea(text: string): boolean {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.setAttribute("aria-hidden", "true");
  area.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none";
  document.body.appendChild(area);
  try {
    area.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
    previous?.focus({ preventScroll: true });
  }
}
