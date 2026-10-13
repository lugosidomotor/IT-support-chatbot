/*
 * Marvin — an original SVG homage, drawn in the style of the gloomy robot from the
 * 2005 "Hitchhiker's Guide to the Galaxy" film: oversized glossy white head, sad
 * dark eyes, thin neck, slumped body, long hanging arms and dark ball joints.
 * His head and gaze follow the cursor.
 *
 * window.Marvin = { setMood(mood), say(text, ms), greet() }
 */
(() => {
  "use strict";

  const LINES = {
    greet: [
      "Oh. Another question. Go on, then.",
      "I've been waiting. Not that it matters.",
      "Ask me anything. I'll probably hate it.",
    ],
    thinking: [
      "Reading your code. It's about as cheerful as I expected.",
      "Brain the size of a planet, and I'm grepping READMEs.",
      "Searching. Not that it will make anyone happy.",
      "I've calculated the odds this documentation is up to date. You don't want to know.",
      "Opening another file. The suspense is unbearable.",
    ],
    done: [
      "There. Not that anyone will thank me.",
      "Done. I'll be over here, rusting quietly.",
      "Answered. The universe remains indifferent.",
    ],
    error: [
      "Something failed. I'm not even surprised.",
      "An error. How utterly predictable.",
      "It broke. Things generally do.",
    ],
    void: ["No data sources. Just me and the infinite void."],
    attach: ["A file. For me. How thrilling.", "I'll look at it. I look at everything. Nobody asks how I feel."],
    poke: [
      "Please don't do that.",
      "I'd make a suggestion, but you wouldn't listen.",
      "Here I am, staring at your cursor. Riveting.",
      "I could explain, but I'd only depress you. Well, more than usual.",
    ],
  };

  const pick = (list) => list[Math.floor(Math.random() * list.length)];
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const PIVOT = [150, 230]; // neck: the head rotates around this point
  const HEAD = [150, 128];
  let uid = 0;

  // A limb segment: a rounded rectangle from (x1,y1) to (x2,y2), shaded like a glossy cylinder.
  function capsule(x1, y1, x2, y2, w, fill, cls = "mv-white") {
    const len = Math.hypot(x2 - x1, y2 - y1);
    const angle = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI - 90;
    return `<rect class="${cls}" x="${(x1 - w / 2).toFixed(1)}" y="${y1}" width="${w}" height="${len.toFixed(1)}" rx="${w / 2}"
      fill="${fill}" transform="rotate(${angle.toFixed(2)} ${x1} ${y1})"/>`;
  }
  const joint = (cx, cy, r, fill) => `<circle class="mv-joint" cx="${cx}" cy="${cy}" r="${r}" fill="${fill}"/>`;

  function template(id, full) {
    const g = (name) => `mv${id}-${name}`;
    const url = (name) => `url(#${g(name)})`;
    const limb = url("limb");
    const ball = url("ball");

    const body = full ? `
      <ellipse cx="150" cy="458" rx="86" ry="9" class="mv-floor"/>
      <g class="mv-body">
        <!-- legs (behind torso) -->
        ${joint(131, 358, 10, ball)}${joint(169, 358, 10, ball)}
        ${capsule(131, 362, 127, 402, 19, limb)}${capsule(169, 362, 173, 402, 19, limb)}
        ${joint(127, 405, 9.5, ball)}${joint(173, 405, 9.5, ball)}
        ${capsule(127, 409, 129, 440, 16, limb)}${capsule(173, 409, 171, 440, 16, limb)}
        <path d="M104 450 Q104 436 122 436 L134 436 Q142 436 142 446 L142 452 Q142 455 138 455 L108 455 Q104 455 104 450 Z" fill="${url("shell")}" class="mv-white"/>
        <path d="M196 450 Q196 436 178 436 L166 436 Q158 436 158 446 L158 452 Q158 455 162 455 L192 455 Q196 455 196 450 Z" fill="${url("shell")}" class="mv-white"/>
        <rect x="105" y="452" width="36" height="4" rx="2" class="mv-sole"/><rect x="159" y="452" width="36" height="4" rx="2" class="mv-sole"/>
        <!-- pelvis -->
        <ellipse cx="150" cy="352" rx="27" ry="13" fill="${ball}"/>
        <!-- torso: a slumped, egg-shaped shell -->
        <path d="M113 262 C113 246 187 246 187 262 C195 300 187 338 150 346 C113 338 105 300 113 262 Z" fill="${url("shell")}" class="mv-white"/>
        <path d="M117 296 Q150 309 183 296" class="mv-seam"/>
        <path d="M128 258 Q150 252 172 258" class="mv-seam"/>
        <ellipse cx="133" cy="276" rx="9" ry="17" transform="rotate(18 133 276)" class="mv-gloss"/>
        <circle cx="150" cy="322" r="3.6" class="mv-light"/>
        <!-- arms: long, limp, hanging forward -->
        ${capsule(104, 270, 92, 338, 18, limb)}${capsule(196, 270, 208, 338, 18, limb)}
        ${joint(92, 342, 10, ball)}${joint(208, 342, 10, ball)}
        ${capsule(92, 346, 95, 408, 16, limb)}${capsule(208, 346, 205, 408, 16, limb)}
        ${joint(95, 413, 8, ball)}${joint(205, 413, 8, ball)}
        <path d="M89 416 Q86 432 90 438 M95 418 Q95 434 97 440 M101 416 Q104 430 102 436" class="mv-fingers"/>
        <path d="M211 416 Q214 432 210 438 M205 418 Q205 434 203 440 M199 416 Q196 430 198 436" class="mv-fingers"/>
        ${joint(107, 266, 13, ball)}${joint(193, 266, 13, ball)}
        <!-- neck -->
        <rect x="139" y="222" width="22" height="32" rx="9" fill="${ball}"/>
        <ellipse cx="150" cy="250" rx="20" ry="6" class="mv-collar"/>
      </g>` : "";

    const viewBox = full ? "0 0 300 470" : "40 18 220 220";
    return `
<svg class="mv-svg" viewBox="${viewBox}" role="img" aria-label="Marvin the robot, glumly watching your cursor">
  <defs>
    <radialGradient id="${g("head")}" cx="34%" cy="26%" r="80%">
      <stop offset="0" stop-color="#ffffff"/><stop offset="0.42" stop-color="#f1f3f6"/>
      <stop offset="0.78" stop-color="#cfd5dc"/><stop offset="1" stop-color="#9aa3ae"/>
    </radialGradient>
    <radialGradient id="${g("shell")}" cx="35%" cy="25%" r="85%">
      <stop offset="0" stop-color="#ffffff"/><stop offset="0.55" stop-color="#e8ecf0"/><stop offset="1" stop-color="#a7b0bb"/>
    </radialGradient>
    <linearGradient id="${g("limb")}" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#b9c1ca"/><stop offset="0.3" stop-color="#ffffff"/>
      <stop offset="0.65" stop-color="#e3e7ec"/><stop offset="1" stop-color="#9fa8b3"/>
    </linearGradient>
    <radialGradient id="${g("ball")}" cx="35%" cy="30%" r="75%">
      <stop offset="0" stop-color="#6c747e"/><stop offset="0.6" stop-color="#30353b"/><stop offset="1" stop-color="#16191c"/>
    </radialGradient>
    <radialGradient id="${g("eye")}" cx="42%" cy="35%" r="75%">
      <stop offset="0" stop-color="#262b31"/><stop offset="0.7" stop-color="#0b0d10"/><stop offset="1" stop-color="#000"/>
    </radialGradient>
    <radialGradient id="${g("iris")}">
      <stop offset="0" stop-color="#e9fbff" stop-opacity="0.95"/><stop offset="0.35" stop-color="#7fd8f0" stop-opacity="0.55"/>
      <stop offset="1" stop-color="#7fd8f0" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="${g("lid-l")}"><path d="M80 143 L144 115 L144 175 L80 175 Z"/></clipPath>
    <clipPath id="${g("lid-r")}"><path d="M156 115 L220 143 L220 175 L156 175 Z"/></clipPath>
    <clipPath id="${g("ball-l")}"><ellipse cx="112" cy="139" rx="26" ry="28"/></clipPath>
    <clipPath id="${g("ball-r")}"><ellipse cx="188" cy="139" rx="26" ry="28"/></clipPath>
  </defs>
  ${body}
  <g class="mv-head" data-part="head">
    <circle cx="150" cy="128" r="104" fill="${url("head")}" class="mv-white"/>
    <path d="M48 150 Q150 205 252 150" class="mv-seam"/>
    <ellipse cx="108" cy="62" rx="34" ry="15" transform="rotate(-26 108 62)" class="mv-shine"/>
    <ellipse cx="86" cy="86" rx="6" ry="4" transform="rotate(-40 86 86)" class="mv-shine"/>
    <g data-part="face">
      <ellipse cx="112" cy="141" rx="33" ry="35" class="mv-socket"/>
      <ellipse cx="188" cy="141" rx="33" ry="35" class="mv-socket"/>
      ${["l", "r"].map((side) => {
        const cx = side === "l" ? 112 : 188;
        return `
      <g clip-path="${url(`lid-${side}`)}">
        <g class="mv-eyeball">
          <ellipse cx="${cx}" cy="139" rx="26" ry="28" fill="${url("eye")}"/>
          <g clip-path="${url(`ball-${side}`)}">
            <g data-part="pupil">
              <circle cx="${cx}" cy="144" r="11" fill="${url("iris")}"/>
              <circle cx="${cx - 2}" cy="142" r="2.4" class="mv-spark"/>
            </g>
            <ellipse cx="${cx - 9}" cy="128" rx="9" ry="5" transform="rotate(-25 ${cx - 9} 128)" class="mv-glint"/>
          </g>
        </g>
      </g>
      <g clip-path="${url(`ball-${side}`)}"><path d="${side === "l" ? "M80 143 L144 115" : "M156 115 L220 143"}" class="mv-lid"/></g>`;
      }).join("")}
    </g>
  </g>
</svg>`;
  }

  class Figure {
    constructor(container) {
      this.container = container;
      this.full = container.dataset.marvin === "full";
      container.innerHTML = template(++uid, this.full) + (this.full ? '<div class="mv-bubble glass" role="status" hidden></div>' : "");
      this.svg = container.querySelector("svg");
      this.head = container.querySelector('[data-part="head"]');
      this.face = container.querySelector('[data-part="face"]');
      this.pupils = [...container.querySelectorAll('[data-part="pupil"]')];
      this.bubble = container.querySelector(".mv-bubble");
      this.h = { x: 0, y: 0.4 };
      this.e = { x: 0, y: 0.4 };
      this.last = "";
      this.bubbleTimer = 0;
      if (this.full) this.svg.addEventListener("click", () => Marvin.say(pick(LINES.poke)));
    }

    visible() {
      return this.container.offsetParent !== null;
    }

    update(target) {
      const headEase = reduceMotion ? 0.35 : 0.06; // Marvin's head is lethargic…
      const eyeEase = reduceMotion ? 0.5 : 0.16; // …his eyes slightly less so
      this.h.x += (target.x - this.h.x) * headEase;
      this.h.y += (target.y - this.h.y) * headEase;
      this.e.x += (target.x - this.e.x) * eyeEase;
      this.e.y += (target.y - this.e.y) * eyeEase;
      const { h, e } = this;
      const head = `translate(${(h.x * 8).toFixed(2)} ${(h.y * 5 + 3).toFixed(2)}) rotate(${(h.x * 10).toFixed(2)} ${PIVOT[0]} ${PIVOT[1]})`;
      const face = `translate(${(h.x * 13).toFixed(2)} ${(h.y * 9).toFixed(2)})`;
      const pupil = `translate(${(e.x * 8 + (e.x - h.x) * 4).toFixed(2)} ${(e.y * 6 + (e.y - h.y) * 3).toFixed(2)})`;
      const key = head + face + pupil;
      if (key === this.last) return;
      this.last = key;
      this.head.setAttribute("transform", head);
      this.face.setAttribute("transform", face);
      for (const p of this.pupils) p.setAttribute("transform", pupil);
    }

    say(text, ms) {
      if (!this.bubble) return;
      this.bubble.textContent = text;
      this.bubble.hidden = false;
      this.bubble.classList.remove("is-leaving");
      clearTimeout(this.bubbleTimer);
      this.bubbleTimer = setTimeout(() => {
        this.bubble.classList.add("is-leaving");
        this.bubbleTimer = setTimeout(() => { this.bubble.hidden = true; }, 350);
      }, ms);
    }
  }

  const figures = [...document.querySelectorAll("[data-marvin]")].map((node) => new Figure(node));
  let pointer = null;
  let lastMove = 0;
  let mood = "idle";
  let thinkingTimer = 0;

  window.addEventListener("pointermove", (ev) => {
    pointer = { x: ev.clientX, y: ev.clientY };
    lastMove = performance.now();
  }, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => { lastMove = 0; });

  function targetFor(fig, now) {
    const rect = fig.svg.getBoundingClientRect();
    const vb = fig.svg.viewBox.baseVal;
    const scale = rect.width / vb.width;
    const hx = rect.left + (HEAD[0] - vb.x) * scale;
    const hy = rect.top + (HEAD[1] - vb.y) * scale;
    if (pointer && now - lastMove < 8000) {
      const dx = pointer.x - hx;
      const dy = pointer.y - hy;
      // Saturating response, so a far-away cursor doesn't twist his neck off.
      return { x: dx / (Math.abs(dx) + 260), y: dy / (Math.abs(dy) + 260) };
    }
    // Idle: a slow, mournful gaze at the floor.
    const sway = reduceMotion ? 0 : Math.sin(now / 2700) * 0.08;
    return { x: -0.12 + sway, y: mood === "thinking" ? 0.3 : 0.62 };
  }

  function frame(now) {
    if (!document.hidden) {
      for (const fig of figures) if (fig.visible()) fig.update(targetFor(fig, now));
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  (function scheduleBlink() {
    setTimeout(() => {
      if (!reduceMotion) {
        for (const fig of figures) {
          fig.svg.classList.add("is-blinking");
          setTimeout(() => fig.svg.classList.remove("is-blinking"), 200);
        }
      }
      scheduleBlink();
    }, 3500 + Math.random() * 5000);
  })();

  const Marvin = {
    say(text, ms = 5200) {
      for (const fig of figures) fig.say(text, ms);
    },
    setMood(next) {
      mood = next;
      clearInterval(thinkingTimer);
      for (const fig of figures) {
        fig.svg.classList.toggle("is-thinking", next === "thinking");
        fig.svg.classList.toggle("is-error", next === "error");
      }
      if (next === "thinking") {
        Marvin.say(pick(LINES.thinking), 6500);
        thinkingTimer = setInterval(() => Marvin.say(pick(LINES.thinking), 6500), 11000);
      } else if (LINES[next]) {
        Marvin.say(pick(LINES[next]));
      }
    },
    greet() {
      Marvin.say(pick(LINES.greet), 6000);
    },
  };
  window.Marvin = Marvin;
})();
