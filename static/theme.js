(() => {
  const root = document.documentElement, btn = document.getElementById("themeToggle");
  const current = () => root.dataset.theme || "dark";
  const apply = () => {
    const light = current() === "light";
    document.querySelector('meta[name="theme-color"]').content = light ? "#FAFAFA" : "#080808";
    btn.setAttribute("aria-pressed", String(light));
    btn.title = light ? "Koyu temaya geç" : "Açık temaya geç";
  };
  btn.addEventListener("click", () => {
    const next = current() === "light" ? "dark" : "light";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch {}
    apply();
  });
  apply();
})();
