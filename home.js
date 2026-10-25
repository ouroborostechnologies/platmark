// Keeps the pinned phone in step with the scroll: the chapter at the reading line picks the
// screenshot and the caption, and on narrow screens text fades as it slides under the phone.
(() => {
  const chapters = [...document.querySelectorAll(".chapter")];
  const shots = [...document.querySelectorAll(".screen img")];
  const device = document.querySelector(".device");
  const caption = document.querySelector(".caption");
  const place = caption.querySelector(".place");
  const coords = caption.querySelector(".coords");
  const terrain = document.querySelector(".terrain");
  const fading = [...document.querySelectorAll(".copy > *")];

  const narrow = matchMedia("(max-width: 759px)");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const fadeDistance = 72;

  let active = chapters[0];
  let captionTimer;

  function show(chapter) {
    if (chapter === active) return;
    active = chapter;
    for (const img of shots) {
      const on = img.dataset.shot === chapter.dataset.shot;
      img.classList.toggle("is-active", on);
      img.toggleAttribute("aria-hidden", !on);
    }
    if (place.textContent === chapter.dataset.place) return;
    clearTimeout(captionTimer);
    const swap = () => {
      place.textContent = chapter.dataset.place;
      coords.textContent = chapter.dataset.coords;
      caption.classList.remove("is-changing");
    };
    if (still.matches) return swap();
    caption.classList.add("is-changing");
    captionTimer = setTimeout(swap, 250);
  }

  function update() {
    const vh = window.innerHeight;
    const phoneBottom = device.getBoundingClientRect().bottom;

    // The reading line: the middle of the window beside the phone, or low in the
    // strip below it when the phone sits on top, so a heading swaps the screen as it arrives.
    const line = narrow.matches ? phoneBottom + (vh - phoneBottom) * 0.7 : vh * 0.5;
    let current = chapters[0];
    for (const chapter of chapters) {
      if (chapter.getBoundingClientRect().top <= line) current = chapter;
    }
    show(current);

    if (narrow.matches) {
      for (const el of fading) {
        const top = el.getBoundingClientRect().top;
        const opacity = Math.min(Math.max((top - phoneBottom) / fadeDistance, 0), 1);
        el.style.opacity = opacity;
      }
    }

    if (!still.matches) {
      const range = document.documentElement.scrollHeight - vh;
      const travel = terrain.offsetHeight - vh;
      const progress = range > 0 ? window.scrollY / range : 0;
      terrain.style.transform = `translateY(${-Math.max(travel, 0) * progress}px)`;
    }
  }

  let queued = false;
  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      update();
    });
  }

  narrow.addEventListener("change", () => {
    for (const el of fading) el.style.opacity = "";
    schedule();
  });
  still.addEventListener("change", () => {
    terrain.style.transform = "";
    schedule();
  });
  addEventListener("scroll", schedule, { passive: true });
  addEventListener("resize", schedule);
  update();
})();
