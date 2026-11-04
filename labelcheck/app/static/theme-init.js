// LabelCheck theme initialization (runs before page render to prevent FOUC)
(function() {
  try {
    const saved = localStorage.getItem("labelcheck_theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const theme = (saved === "light" || saved === "dark") ? saved : (prefersDark ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  } catch (_) {}
})();

