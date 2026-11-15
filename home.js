// Shows the screenshot of whichever scene is across the middle of the window, fading it in over
// the others so the screen never dims to black partway through, and enlarges it when tapped.
(() => {
  const screen = document.querySelector(".screen");
  const screens = new Map([...screen.querySelectorAll("img")].map((img) => [img.dataset.scene, img]));
  let active = screen.querySelector(".is-shown");
  let topLayer = 0;

  const layer = (img) => Number(img.style.zIndex);

  // A hidden screenshot rises to the top and fades in. One still showing underneath, because a
  // fade was reversed partway, stays put and the screenshots above it fade away instead.
  function show(scene) {
    const next = screens.get(scene);
    if (next === active) return;
    active = next;
    if (getComputedStyle(next).opacity === "0") next.style.zIndex = ++topLayer;
    for (const img of screens.values()) {
      if (layer(img) > layer(next)) img.classList.remove("is-shown");
    }
    next.classList.add("is-shown");
  }

  // Once the active screenshot is fully in, the ones it covers are hidden.
  screen.addEventListener("transitionend", (event) => {
    if (event.target !== active) return;
    for (const img of screens.values()) {
      if (img !== active) img.classList.remove("is-shown");
    }
  });

  const middle = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) show(entry.target.dataset.scene);
      }
    },
    { rootMargin: "-50% 0px -50% 0px" },
  );
  for (const scene of document.querySelectorAll(".scene")) middle.observe(scene);

  // Tapping the device lifts its screenshot out of the bezel into a dialog over the darkened page.
  const device = document.querySelector(".device");
  const viewer = document.querySelector(".viewer");
  const enlarged = viewer.querySelector("img");
  const motion = {
    duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 400,
    easing: "cubic-bezier(0.2, 0, 0, 1)",
    fill: "forwards",
  };
  let closing = false;

  // The enlarged screenshot, scaled and moved back onto the device's screen.
  function onScreen() {
    const from = screen.getBoundingClientRect();
    const to = enlarged.getBoundingClientRect();
    return `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})`;
  }

  device.querySelector(".enlarge").addEventListener("click", async () => {
    enlarged.src = active.src;
    enlarged.alt = active.alt;
    await enlarged.decode();
    viewer.showModal();
    device.classList.add("is-enlarged");
    enlarged.animate([{ transform: onScreen() }, { transform: "none" }], motion);
    viewer.animate([{ backgroundColor: "transparent" }, {}], motion);
  });

  async function close() {
    if (closing) return;
    closing = true;
    for (const animation of [...enlarged.getAnimations(), ...viewer.getAnimations()]) animation.cancel();
    device.classList.remove("is-enlarged");
    const animations = [
      enlarged.animate([{ transform: "none" }, { transform: onScreen() }], motion),
      viewer.animate([{}, { backgroundColor: "transparent" }], motion),
    ];
    await Promise.all(animations.map((animation) => animation.finished));
    viewer.close();
    for (const animation of animations) animation.cancel();
    closing = false;
  }

  viewer.addEventListener("click", close);
  viewer.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
})();
