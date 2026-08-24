(() => {
  const showStartupFailure = () => {
    const bootstrap = document.getElementById("share-sentinel-boot");
    const detail = document.getElementById("share-sentinel-boot-detail");
    if (!bootstrap || !detail) return;

    bootstrap.setAttribute("role", "alert");
    const heading = bootstrap.querySelector("strong");
    if (heading) heading.textContent = "Share Sentinel could not start";
    detail.textContent =
      "The browser could not load or run the application files. Hard-refresh this page. If the problem continues, run ./scripts/doctor.sh on the server and inspect the browser console.";
  };

  window.setTimeout(showStartupFailure, 10_000);
  window.addEventListener("error", showStartupFailure, true);
  window.addEventListener("unhandledrejection", showStartupFailure);
})();
