# LocalJSON — local card data for Proxyshop

A Proxyshop plugin that lets you pull card data from a **local JSON file** instead of
(or before) the **Scryfall API**. Useful for fully custom/proxy cards, offline rendering,
or overriding what Scryfall returns for specific cards.

It works with **every existing template** and the Custom Creator is left untouched — the
plugin only changes *where the card data comes from*, not how cards are rendered.

## How it works

Proxyshop fetches each card's data through one function, `src.cards.get_card_data(...)`,
which returns a Scryfall-shaped dictionary that the layout/template code consumes. This
plugin wraps that function at startup. When local-JSON mode is on and a matching card is
found in your data file, the local entry is served; otherwise behaviour depends on the
mode below. No core Proxyshop files are modified, so app updates won't clobber it.

## Install

1. Copy the `LocalJSON` folder into your Proxyshop `plugins/` directory:
   `Proxyshop/plugins/LocalJSON/`
2. Put your cards in `plugins/LocalJSON/cards.json`, then render as usual (drop art in
   `art/`, **Render All** / **Render Target**). The art filename still drives the card
   name and the optional `[SET]` / `{num}` tags, exactly like normal Proxyshop.
3. Launch Proxyshop and start rendering. The console prints `[LocalJSON] Loaded *X* card(s) from cards.json`
 

Out of the box `cards.json` contains the french version of the original dual lands for example purpose, so only these changes until you add cards.

## The option (settings.json)

`plugins/LocalJSON/settings.json` is the on/off switch and is re-read automatically when
you change it (no restart needed):

| Key            | Values                         | Meaning                                                              |
| -------------- | ------------------------------ | -------------------------------------------------------------------- |
| `enabled`      | `true` / `false`               | Master switch for the plugin.                                        |
| `mode`         | `merge` / `auto` / `strict` / `off` | `merge`: always query Scryfall, then overlay the non-null/non-empty JSON fields. `auto`: JSON first, Scryfall fallback. `strict`: JSON only (no net). `off`: Scryfall only. |
| `data_file`    | path                           | JSON data file: absolute, or relative to this folder, or to the working dir. |
| `match_set`    | `true` / `false`               | When several entries share a name, prefer the one matching the `[SET]` tag. |
| `match_number` | `true` / `false`               | Likewise prefer the entry matching the `{num}` tag.                  |
| `log`          | `true` / `false`               | Print a short line to the Proxyshop console on hits.                 |

## Merge mode (the recommended default)

In `merge` mode Proxyshop fetches the real card from Scryfall first, then overlays
**only the fields you actually filled in** — any value that is `null`, an empty string,
or an empty list/array is ignored and Scryfall's value is kept. This lets you tweak a
real card (a translated name, custom flavour text, a forced set code…) without redefining
the whole thing.

Example — French display for a real card, everything else from Scryfall:

```json
{
  "name": "Tundra",
  "printed_name": "Toundra",
  "printed_type_line": "Terrain : Plaine et île",
  "printed_text": "{T} : Ajoutez {W} ou {U}.",
  "lang": "fr",
  "set": "LEA"
}
```

Notes:
- `name` is used only to match the card; it is **not** overlaid (so it can't clobber
  Scryfall's canonical name, which matters for double-faced cards). To change the
  displayed name, set `printed_name`.
- Double-faced cards merge per face: provide a `card_faces` array and entry *i* overlays
  Scryfall's face *i*. Leave a field out of a face to keep Scryfall's value.
- Merge can't blank out a field (empty = "keep Scryfall"). If you need to truly clear or
  fully define a card, use `strict`/`auto` with a complete entry instead.

## The data file (cards.json)

The top level can be any of:

- a **list** of entries,
- `{ "cards": [ ... ] }` (or `{ "data": [ ... ] }`), or
- a **map** `{ "Card Name": { ...entry... }, ... }`.

Each entry only strictly needs a `name`. Everything else has sensible defaults. You can
mix two styles freely:

### Friendly schema (for custom/proxy cards)

```json
{
  "name": "Grizzly Bears",
  "mana_cost": "{1}{G}",
  "type_line": "Creature — Bear",
  "oracle_text": "",
  "power": 2,
  "toughness": 2,
  "rarity": "common",
  "set": "MTG",
  "collector_number": "1",
  "artist": "Your Name"
}
```

Convenient aliases are accepted: `type`→`type_line`, `text`/`rules`→`oracle_text`,
`flavor`→`flavor_text`, `cost`/`mana`→`mana_cost`, `number`→`collector_number`,
`faces`→`card_faces`. If you omit `color_identity` it's derived from the mana cost.

Double-faced cards (transform / MDFC) use `card_faces` and a `layout`:

```json
{
  "name": "Front Name",
  "layout": "transform",
  "card_faces": [
    { "name": "Front Name", "mana_cost": "{2}{R}", "type_line": "Creature — Shaman", "oracle_text": "...", "power": 2, "toughness": 2 },
    { "name": "Back Name",  "type_line": "Creature — Elemental", "oracle_text": "Trample, haste.", "power": 5, "toughness": 4 }
  ]
}
```

Use `"layout": "modal_dfc"` for MDFCs. Planeswalkers: give a `type_line` containing
`Planeswalker` and put each ability on its own line in `oracle_text` — the layout parses
them automatically.

### Raw Scryfall objects

Any entry that is already a Scryfall card object (e.g. it has `"object": "card"`) is passed
through untouched. So you can paste real Scryfall JSON straight in — handy for capturing a
specific printing once and rendering it offline forever.

## Tips

- Matching is by **normalised name** (case/accents/spacing ignored), the same way
  Proxyshop matches art filenames, so `art/Lightning Bolt.jpg` finds `"Lightning Bolt"`.
  The filename is checked against **`printed_name` first, then `name`, then `aliases`**, so
  in every mode a French file (`Toundra.jpg`) and an English file (`Tundra.jpg`) both resolve
  to the same entry.
- To force a specific printing when you have duplicates, name your art with tags:
  `Brainstorm [SLD] {175}.jpg` and keep `match_set` / `match_number` on.
- `strict` mode is the one to use if you want guaranteed-offline rendering and an error
  on any card that isn't in your file.

## Uninstall

Delete the `plugins/LocalJSON/` folder (or set `"enabled": false`).
