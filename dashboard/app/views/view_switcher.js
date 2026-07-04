// Renders a small fixed pill at the top-left of the page that
// navigates to the dashboard's counterpart view (single-trip ↔
// average-delay). The label and href come from data.json's
// `counterpart_label` and `counterpart_url`; both are set by the
// Python build scripts.

export function mountViewSwitcher(data) {
  if (!data || !data.counterpart_url) return;
  const a = document.createElement("a");
  a.className = "view-switcher";
  a.href = data.counterpart_url;
  a.textContent = data.counterpart_label || "Switch view";
  document.body.appendChild(a);
}
