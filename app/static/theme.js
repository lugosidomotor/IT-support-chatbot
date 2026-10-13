/*
 * Light / dark theme. Loaded in <head> (blocking) so the right theme is applied before
 * the first paint. Follows the OS setting until the user picks one; the choice is remembered.
 * window.DSATheme = { get(), toggle() }
 */
(() => {
  "use strict";

  const KEY = "dsa-theme";
  const COLORS = { light: "#eef1f7", dark: "#07080c" };
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let chosen = null;
  try { chosen = localStorage.getItem(KEY); } catch { /* storage unavailable */ }
  if (chosen !== "light" && chosen !== "dark") chosen = null;

  const resolve = () => chosen || (media.matches ? "dark" : "light");
  function apply(theme) {
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", COLORS[theme]);
    document.dispatchEvent(new CustomEvent("dsa:theme", { detail: theme }));
  }

  apply(resolve());
  media.addEventListener("change", () => { if (!chosen) apply(resolve()); });

  window.DSATheme = {
    get: () => document.documentElement.dataset.theme,
    toggle() {
      chosen = resolve() === "dark" ? "light" : "dark";
      try { localStorage.setItem(KEY, chosen); } catch { /* storage unavailable */ }
      apply(chosen);
      return chosen;
    },
  };
})();
