// Copy buttons next to install commands.
document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const text = document.getElementById(button.dataset.copy).textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
      const label = button.textContent;
      button.textContent = button.dataset.done || "copied";
      setTimeout(() => (button.textContent = label), 1500);
    } catch (e) {
      /* clipboard unavailable: the command stays selectable */
    }
  });
});
