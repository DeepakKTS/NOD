# Nod Console — Design system

The brief pins the visual direction: Apple iOS liquid glass, sleek, aesthetic. That is
followed exactly. The design thinking is spent on grounding it in the subject matter
rather than on choosing a style.

## 1. Subject and job

The subject is **silence under negotiation**. The console shows a machine deciding, many
times a second, whether a person has finished speaking. The primary job is to make an
invisible timing decision visible and legible at a glance, to an operations lead who is
not an engineer.

That gives the design its one strong idea.

## 2. The hero: the Floor Meter

Everything else is quiet so this can be loud.

A single horizontal capsule at the top of the call lane. It represents the silence the
agent will currently tolerate before it takes the floor. When the caller stops speaking,
it begins to fill, left to right, in real time. If the caller resumes, it empties. If it
fills, the agent speaks.

- The fill is warm amber while the caller holds the floor.
- The capsule's **length** is `max_turn_silence`. When the controller widens the window,
  the capsule visibly grows, with a spring. That single animation is the entire product
  thesis in one gesture, and it is the only non-user-triggered motion in the app.
- A hairline tick marks `min_turn_silence`; a second, softer tick marks the confidence
  threshold crossing when it occurs.

Nothing else in the interface animates on its own.

## 3. Tokens

### Colour

Dark-first. The background is "room tone": a deep, slightly blue field that reads as an
acoustic space rather than a dashboard chrome.

```css
--ink-900:  #0A0E14;   /* base field */
--ink-800:  #121821;   /* recessed */
--ink-700:  #1B2430;   /* raised solid fallback */

--floor:    #F0A63C;   /* caller holds the floor — amber */
--agent:    #64D2FF;   /* machine action — iOS system cyan */
--cut:      #FF453A;   /* a cut happened — iOS system red */
--calm:     #30D158;   /* healthy, within budget — iOS system green */

--text-1:   rgba(255,255,255,0.92);
--text-2:   rgba(255,255,255,0.62);
--text-3:   rgba(255,255,255,0.38);
```

Four functional colours, and each one means exactly one thing. Amber is never decorative;
if something is amber, the caller has the floor.

Light mode inverts the field to `#F2F4F7` / `#FFFFFF` and keeps the four accents, with
text tokens swapped to near-black alphas. Both modes ship; dark is the default because
the console is used alongside a live call.

### Glass materials

Three materials, no more. Every surface picks one.

```css
--glass-thin: {
  background: rgba(255,255,255,0.05);
  backdrop-filter: blur(20px) saturate(160%);
  border: 0.5px solid rgba(255,255,255,0.10);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.14);
}
--glass-regular: {
  background: rgba(255,255,255,0.08);
  backdrop-filter: blur(28px) saturate(180%);
  border: 0.5px solid rgba(255,255,255,0.16);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.22),
              0 12px 40px rgba(0,0,0,0.40);
}
--glass-thick: {            /* modals and the settings sheet only */
  background: rgba(255,255,255,0.12);
  backdrop-filter: blur(40px) saturate(200%);
  border: 0.5px solid rgba(255,255,255,0.22);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.28),
              0 24px 64px rgba(0,0,0,0.55);
}
```

The specular inset highlight on the top edge is what separates real liquid glass from a
translucent rectangle. Do not omit it. Do not add a bottom inset highlight.

### Radius, spacing, type

```css
--r-card: 22px;   --r-control: 14px;   --r-pill: 999px;
--space: 4px base; use 8 12 16 24 32 48
```

Type: `-apple-system, "SF Pro Text", "SF Pro Display", Inter, system-ui, sans-serif`.
One family. Scale: 34 / 26 / 20 / 17 / 15 / 13, weights 400 and 600 only.

**Every live-changing number uses `font-variant-numeric: tabular-nums`.** The config strip
updates several times a call; proportional figures make values jump horizontally and the
whole strip reads as noise. This is the single most important typographic decision in the
app and it is specific to this product.

### Motion

```css
--spring: cubic-bezier(0.22, 1, 0.36, 1);
--fast: 180ms;  --base: 320ms;  --slow: 480ms;
```

Motion answers an action or shows a state change. The Floor Meter growing is the one
exception and it is earned: it *is* the state change. No entrance animations, no hover
lifts on cards, no staggered reveals.

## 4. Layout

```
┌──────────────────────────────────────────────────────────────┐
│  Nod            Live call   Replay   Benchmark      ⚙︎  ◉ live │  glass-thin bar
├──────────────────────────────────────────────────────────────┤
│                                                              │
│   ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱   2.6 s          │  ← Floor Meter (hero)
│   Waiting for the caller                                     │
│                                                              │
│   ┌──────────────────────────────┐ ┌──────────────────────┐ │
│   │ transcript, newest at bottom │ │ Listening window     │ │
│   │ words fade in as finalised   │ │  min   400 ms        │ │
│   │                              │ │  max  2 600 ms  ↑    │ │
│   │                              │ │  conf  0.62     ↑    │ │
│   │                              │ │                      │ │
│   │                              │ │ Caller pauses 1.4 s  │ │  ← the reason line
│   │                              │ └──────────────────────┘ │
│   │                              │ ┌──────────────────────┐ │
│   │                              │ │ Cuts    1            │ │
│   │                              │ │ Reply   p90 740 ms   │ │
│   └──────────────────────────────┘ └──────────────────────┘ │
│                                                              │
│   ~~~~~ waveform (canvas, no glass behind it) ~~~~~          │
└──────────────────────────────────────────────────────────────┘
```

Left-aligned throughout. Content column maxes at 1200 px. The transcript is the only
scrolling region.

Screens: **Live call**, **Replay** (two panes, stock vs Nod, shared scrubber),
**Benchmark** (Pareto chart and arm table), **Settings** (sheet, glass-thick).

## 5. Components

| Component | Material | Notes |
|---|---|---|
| Nav bar | thin | pinned, 52 px, `◉ live` dot uses `--calm`, `--cut` when degraded |
| Floor Meter | none (solid on field) | the hero; must not sit on glass or the fill loses contrast |
| Config strip | regular | one row per parameter, tabular figures, arrow glyph on change |
| Reason line | none | 13 px `--text-2`, one sentence, plain language: "Caller pauses 1.4 s", never "p90=1400" |
| Transcript | regular | word appears at `word_is_final`; a cut is marked with a thin `--cut` rule, not an icon |
| Metric tiles | thin | two per row, numbers 26 px |
| Voice picker | thick sheet | provider group, voice rows with latency and cost class, live preview button |
| Waveform | canvas, no backdrop-filter | performance; see §7 |
| Pareto chart | thin | hand-rolled SVG, axis labels 13 px, arms as labelled points with error bars |

## 6. Copy rules

Plain language, active voice, sentence case. The interface talks about people and time,
not about parameters.

- "Waiting for the caller" not "Endpointing pending"
- "Caller pauses 1.4 s" not "g_p90 = 1400 ms"
- "Widened the listening window" not "max_turn_silence patched"
- "Reading an ID number" for the context axis
- Empty benchmark view: "No runs yet. Run `make bench` to generate one." Direction, not mood.
- Degraded banner: "This model does not expose a confidence threshold. Nod is adjusting
  silence only." State what happened and what it means.

The raw parameter names stay available, one tap away, in the strip's expanded state. The
operations lead sees sentences; the engineer can see numbers.

## 7. Performance budget — non-negotiable

`backdrop-filter` is expensive and this app renders a live waveform.

- Maximum **6** blurred layers composited at once. Count them in review.
- **Never animate `backdrop-filter`, `blur()` radius, or the size of a blurred element.**
  Animate `transform` and `opacity` only.
- The waveform is a `<canvas>` painted in a `requestAnimationFrame` loop and is **not**
  inside a blurred stack and has nothing blurred on top of it.
- Glass surfaces get `contain: paint` and a stable `transform: translateZ(0)` to keep
  them on their own layer.
- Telemetry arrives per partial turn; the UI coalesces to at most 20 updates per second
  through a rAF-batched store. Never render per WebSocket message.
- Frame budget: 16 ms. A CI Lighthouse check fails the build below 55 fps on the live
  call view with a synthetic stream.

## 8. Accessibility — equivalent, not degraded

- `prefers-reduced-transparency`: glass becomes `--ink-700` solid with a 1 px border.
  Contrast improves; nothing is lost.
- `prefers-reduced-motion`: the Floor Meter jumps to its new length instead of springing.
  All other transitions become instant.
- Contrast: all text meets WCAG AA against the **worst-case** backdrop behind the glass,
  not the average. Test with a white backdrop image behind the blur.
- Colour is never the only signal: a cut is a rule plus a label, not just red.
- Full keyboard path, visible focus ring (`--agent`, 2 px, 2 px offset).
- The live region announces config changes politely, at most once every 5 s.

## 9. What this must never become

- Identical rounded cards in a grid with the same shadow under each.
- A gradient wash used as decoration.
- All-caps tracked-out eyebrow labels above every section.
- A monospace face for small data labels. Tabular figures in the text face do that job.
- Emoji as status icons.
